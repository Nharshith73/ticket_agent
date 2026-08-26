"""Dev-utility script for local development and testing resets ONLY.

Clears local triage database state (logs, review_queue, LangGraph SqliteSaver checkpoints/writes)
and resets the processed Google Docs cache (processed_gdocs.json).

NOTE: This script ONLY modifies local app state (triage.db and processed_gdocs.json).
It NEVER interacts with or deletes anything in the real JIRA instance.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from database import DB_PATH, init_db

GDOCS_CACHE_PATH = Path(__file__).with_name("processed_gdocs.json")

# Tables to reset if they exist in the database
TARGET_TABLES = [
    "logs",
    "review_queue",
    "checkpoints",
    "writes",
    "checkpoint_blobs",
    "checkpoint_writes",
]


def reset_local_state(skip_confirm: bool = False) -> None:
    """Clear local database tables and processed gdocs cache."""
    print("=" * 60)
    print("LOCAL DEVELOPMENT RESET UTILITY")
    print("=" * 60)

    db_file = Path(DB_PATH)
    if not db_file.exists():
        print(f"[reset] Database file not found at {DB_PATH}. Initializing database schema...")
        init_db()
        print("[reset] Database initialized successfully.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Find existing tables in the database
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = [row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")]

    tables_to_clear = [t for t in TARGET_TABLES if t in existing_tables]

    # Gather row counts before deletion
    summary = {}
    total_rows = 0
    for table in tables_to_clear:
        cursor.execute(f"SELECT COUNT(*) FROM [{table}]")
        count = cursor.fetchone()[0]
        summary[table] = count
        total_rows += count

    gdocs_cached_count = 0
    if GDOCS_CACHE_PATH.exists():
        try:
            with open(GDOCS_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    gdocs_cached_count = len(data)
        except Exception:
            pass

    print("\nState Summary Before Reset:")
    for table, count in summary.items():
        print(f"  - Database table '{table}': {count} rows")
    print(f"  - Google Docs cache ('{GDOCS_CACHE_PATH.name}'): {gdocs_cached_count} items cached")
    print("-" * 60)

    if not skip_confirm:
        confirm = input("\nAre you sure you want to clear all local state? (y/N): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("[reset] Operation cancelled by user.")
            conn.close()
            return

    # Delete records from target tables without dropping table schemas
    deleted_counts = {}
    for table in tables_to_clear:
        cursor.execute(f"DELETE FROM [{table}]")
        deleted_counts[table] = cursor.rowcount if cursor.rowcount >= 0 else summary[table]

    conn.commit()
    conn.close()

    # Reset processed Google Docs cache file to empty array []
    with open(GDOCS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, indent=2)

    # Ensure schema remains intact
    init_db()

    print("\n[reset] Successfully cleared local state:")
    for table, count in deleted_counts.items():
        print(f"  [x] Cleared {summary.get(table, 0)} rows from table '{table}'")
    print(f"  [x] Reset '{GDOCS_CACHE_PATH.name}' cache to []")
    print("[reset] Call to database.init_db() completed — database schema is intact.")
    print("[reset] Local environment is fresh and ready for triage runs!\n")


def main():
    parser = argparse.ArgumentParser(
        description="Reset local development database and Google Docs cache (Dev tool only)."
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and clear local state immediately.",
    )
    args = parser.parse_args()
    reset_local_state(skip_confirm=args.yes)


if __name__ == "__main__":
    main()
