"""SQLite helpers for dashboard data and durable LangGraph checkpoints."""

import base64
import json
import os
import sqlite3
import uuid
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _get_db_path() -> str:
    if os.environ.get("TRIAGE_DB_PATH"):
        return os.environ["TRIAGE_DB_PATH"]
    if os.environ.get("VERCEL") or os.environ.get("NOW_REGION") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return "/tmp/triage.db"
    return str(Path(__file__).with_name("triage.db"))


DB_PATH = _get_db_path()


def _connect() -> sqlite3.Connection:
    """Return an independent connection suitable for short web/database operations."""
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass
    return connection


def init_db() -> None:
    """Create the dashboard tables. LangGraph creates checkpoint tables itself."""
    try:
        with _connect() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except Exception:
                try:
                    conn.execute("PRAGMA journal_mode = DELETE")
                except Exception:
                    pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    message TEXT NOT NULL,
                    level TEXT NOT NULL CHECK(level IN ('info', 'warning', 'error'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_queue (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL UNIQUE,
                    raw_text TEXT NOT NULL,
                    source_tag TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    category TEXT NOT NULL,
                    urgency TEXT NOT NULL,
                    confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100),
                    due_date TEXT,
                    suggested_assignee TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'rejected')),
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_queue_thread_id ON review_queue(thread_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS judge_verdicts (
                    ticket_id TEXT PRIMARY KEY,
                    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                    reasoning TEXT NOT NULL,
                    flags TEXT NOT NULL,
                    was_human_approved INTEGER NOT NULL,
                    decision_snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            for col in ("due_date", "suggested_assignee"):
                try:
                    conn.execute(f"ALTER TABLE review_queue ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS team_members (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    primary_category TEXT NOT NULL,
                    jira_account_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS member_availability (
                    id TEXT PRIMARY KEY,
                    member_email TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('available', 'ooo', 'vacation')),
                    start_date TEXT,
                    end_date TEXT,
                    notes TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jira_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    jira_url TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    api_token TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
    except Exception as e:
        print(f"[db warning] Could not initialize database on startup: {e}")


def insert_log(timestamp: str, message: str, level: str) -> None:
    """Persist a log event so the dashboard has an audit trail beyond SSE clients."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO logs (timestamp, message, level) VALUES (?, ?, ?)",
            (timestamp, message, level),
        )


def get_recent_logs(limit: int = 100) -> list[dict]:
    """Return recent logs ordered by ID ascending (chronological)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT timestamp, message, level FROM (SELECT id, timestamp, message, level FROM logs ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_or_create_review_item(
    *,
    thread_id: str,
    raw_text: str,
    source_tag: str,
    summary: str,
    category: str,
    urgency: str,
    confidence: int,
    due_date: str | None = None,
    suggested_assignee: str | None = None,
) -> dict:
    """Return the one review card for a graph thread, creating it on first pause.

    A LangGraph interrupt reruns the interrupted node after a human response. The
    unique ``thread_id`` makes that replay idempotent rather than adding a card twice.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_queue WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row:
            return dict(row)

        item_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """
                INSERT INTO review_queue
                    (id, thread_id, raw_text, source_tag, summary, category, urgency,
                     confidence, due_date, suggested_assignee, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    item_id,
                    thread_id,
                    raw_text,
                    source_tag,
                    summary,
                    category,
                    urgency,
                    confidence,
                    due_date,
                    suggested_assignee,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError:
            # Another request reached the pause first; use its review card instead.
            row = conn.execute(
                "SELECT * FROM review_queue WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row:
                return dict(row)
            raise
        row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
        return dict(row)


def get_pending_reviews() -> list[dict]:
    """Return pending review cards, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM review_queue WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_review_by_id(item_id: str) -> dict | None:
    """Return one review item, regardless of its final status."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def get_review_by_thread_id(thread_id: str) -> dict | None:
    """Return the review record associated with one LangGraph thread."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM review_queue WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    return dict(row) if row else None


def set_review_status_if_pending(item_id: str, status: str) -> bool:
    """Atomically record one human decision; return False for stale/double clicks."""
    if status not in {"approved", "rejected"}:
        raise ValueError("status must be 'approved' or 'rejected'")
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE review_queue SET status = ? WHERE id = ? AND status = 'pending'",
            (status, item_id),
        )
    return cursor.rowcount == 1


def reset_review_status_to_pending(item_id: str, expected_status: str) -> None:
    """Reopen a decision if its graph resume could not be started."""
    with _connect() as conn:
        conn.execute(
            "UPDATE review_queue SET status = 'pending' WHERE id = ? AND status = ?",
            (item_id, expected_status),
        )


def save_judge_verdict(
    *,
    ticket_id: str,
    score: int,
    reasoning: str,
    flags: list[str] | str,
    was_human_approved: bool,
    decision_snapshot: dict | str,
) -> dict:
    """Persist an LLM-as-a-judge verdict for a ticket."""
    flags_json = json.dumps(flags) if isinstance(flags, list) else str(flags)
    snapshot_json = (
        json.dumps(decision_snapshot)
        if isinstance(decision_snapshot, dict)
        else str(decision_snapshot)
    )
    created_at = datetime.now(timezone.utc).isoformat()
    human_approved_int = 1 if was_human_approved else 0

    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO judge_verdicts
                (ticket_id, score, reasoning, flags, was_human_approved, decision_snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                score,
                reasoning,
                flags_json,
                human_approved_int,
                snapshot_json,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM judge_verdicts WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return dict(row) if row else {}


def get_judge_verdict(ticket_id: str) -> dict | None:
    """Return the saved judge verdict for one ticket, or None if missing."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM judge_verdicts WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["flags"] = json.loads(d["flags"])
    except Exception:
        d["flags"] = []
    try:
        d["decision_snapshot"] = json.loads(d["decision_snapshot"])
    except Exception:
        pass
    d["was_human_approved"] = bool(d["was_human_approved"])
    return d


# --- Team Members & Availability Helpers ---

def get_all_team_members() -> list[dict]:
    """Return all team members ordered by creation date."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM team_members ORDER BY created_at ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_all_categories() -> list[str]:
    """Return distinct active categories from registered team members, falling back to defaults if empty."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT primary_category FROM team_members WHERE primary_category IS NOT NULL AND primary_category != ''"
        ).fetchall()
    categories = [row["primary_category"].strip().lower() for row in rows if row["primary_category"]]
    if not categories:
        return ["bug", "feature_request", "other"]
    if "other" not in categories:
        categories.append("other")
    return categories



def get_team_members_by_category(category: str) -> list[dict]:
    """Return all team members mapped to a specific category."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM team_members WHERE primary_category = ?", (category,)
        ).fetchall()
    return [dict(row) for row in rows]


def add_team_member(
    name: str,
    email: str,
    primary_category: str,
    jira_account_id: str,
) -> dict:
    """Insert or replace a team member."""
    item_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO team_members (id, name, email, primary_category, jira_account_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name=excluded.name,
                primary_category=excluded.primary_category,
                jira_account_id=excluded.jira_account_id
            """,
            (item_id, name, email.strip().lower(), primary_category, jira_account_id, created_at),
        )
        row = conn.execute("SELECT * FROM team_members WHERE email = ?", (email.strip().lower(),)).fetchone()
        return dict(row) if row else {}


def delete_team_member(member_id: str) -> bool:
    """Delete a team member by ID."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM team_members WHERE id = ?", (member_id,))
    return cursor.rowcount > 0


def get_member_availability(email: str) -> dict | None:
    """Return availability record for a member's email."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM member_availability WHERE member_email = ?", (email.strip().lower(),)
        ).fetchone()
    return dict(row) if row else None


def set_member_availability(
    email: str,
    status: str,
    start_date: str | None = None,
    end_date: str | None = None,
    notes: str | None = None,
) -> dict:
    """Set or update member availability (available, ooo, vacation)."""
    if status not in {"available", "ooo", "vacation"}:
        raise ValueError("Status must be 'available', 'ooo', or 'vacation'")
    item_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO member_availability (id, member_email, status, start_date, end_date, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(member_email) DO UPDATE SET
                status=excluded.status,
                start_date=excluded.start_date,
                end_date=excluded.end_date,
                notes=excluded.notes
            """,
            (item_id, email.strip().lower(), status, start_date, end_date, notes),
        )
        row = conn.execute(
            "SELECT * FROM member_availability WHERE member_email = ?", (email.strip().lower(),)
        ).fetchone()
        return dict(row) if row else {}


def is_user_ooo_today(email: str) -> bool:
    """Check if a user is currently OOO or on vacation."""
    record = get_member_availability(email)
    if not record:
        return False
    status = record.get("status")
    if status in {"ooo", "vacation"}:
        return True
    return False


def get_jira_config() -> dict | None:
    """Return stored in-database Jira credentials if set."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jira_config WHERE id = 1").fetchone()
    return dict(row) if row else None


def save_jira_config(
    jira_url: str,
    project_key: str,
    user_email: str,
    api_token: str,
) -> dict:
    """Store or update dynamic Jira integration credentials."""
    updated_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO jira_config (id, jira_url, project_key, user_email, api_token, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                jira_url=excluded.jira_url,
                project_key=excluded.project_key,
                user_email=excluded.user_email,
                api_token=excluded.api_token,
                updated_at=excluded.updated_at
            """,
            (
                jira_url.strip().rstrip("/"),
                project_key.strip().upper(),
                user_email.strip().lower(),
                api_token.strip(),
                updated_at,
            ),
        )
        row = conn.execute("SELECT * FROM jira_config WHERE id = 1").fetchone()
        return dict(row) if row else {}


init_db()
