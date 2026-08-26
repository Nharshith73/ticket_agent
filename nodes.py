import os
import threading
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt

from database import (
    get_all_categories,
    get_or_create_review_item,
    get_team_members_by_category,
    is_user_ooo_today,
    save_judge_verdict,
)
from jira_client import JiraClient
from log_utils import emit_log
from state import ExtractedIssue, JudgeVerdict, TextState


def _get_fallback_assignee(category: str = "other") -> str:
    """Return configured env assignee or dynamic fallback to connected Jira user account ID."""
    env_key = f"ASSIGNEE_{category.upper()}"
    env_val = os.getenv(env_key) or os.getenv("ASSIGNEE_DEFAULT")
    if env_val:
        return env_val
    return jira_client.my_account_id or "user_acc_triage_lead"


# SLA fallback: how many days out to set the due date when the source text
# doesn't mention one, keyed by extracted urgency.
DUE_DATE_SLA_DAYS = {
    "high": int(os.getenv("SLA_DAYS_HIGH", "1")),
    "medium": int(os.getenv("SLA_DAYS_MEDIUM", "3")),
    "low": int(os.getenv("SLA_DAYS_LOW", "7")),
}


def _resolve_due_date(raw_due_date: str | None, urgency: str) -> tuple[str, bool]:
    """Validate an extracted due date, or fall back to an SLA-based default.

    Returns (due_date_iso, was_defaulted). A due date is only trusted if it
    parses as YYYY-MM-DD and isn't already in the past; anything else (missing,
    malformed, or stale) falls back to the urgency-based SLA default so every
    ticket always has a usable due date.
    """
    if raw_due_date:
        try:
            parsed = datetime.strptime(raw_due_date.strip(), "%Y-%m-%d").date()
            if parsed >= date.today():
                return parsed.isoformat(), False
        except (ValueError, AttributeError):
            pass

    offset_days = DUE_DATE_SLA_DAYS.get(urgency, DUE_DATE_SLA_DAYS["medium"])
    default_date = date.today() + timedelta(days=offset_days)
    return default_date.isoformat(), True

jira_client = JiraClient()
# OpenRouter implements the OpenAI chat-completions API, so ChatOpenAI and the
# existing Pydantic structured-output workflow can be retained unchanged.
openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
if openrouter_api_key:
    llm = ChatOpenAI(
        model=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        api_key=openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
    )
    structured_llm = llm.with_structured_output(ExtractedIssue)
    judge_structured_llm = llm.with_structured_output(JudgeVerdict)
else:
    llm = None
    structured_llm = None
    judge_structured_llm = None


CONFIDENCE_CALIBRATION_INSTRUCTION = """CONFIDENCE SCORE CALIBRATION GUIDE (0-100):
Be strict and realistic when scoring confidence. Use the following rubric:
- 90-100 (High): Clear, detailed report with specific steps, error messages/codes, URL/page context, or explicit deadlines. Example: "Unable to update card on /checkout page, returns 500 error, renewal due today." -> score 90.
- 50-75 (Medium): Mentions a problem area or feature but lacks key details, error logs, or concrete reproduction steps. Example: "Payment page seems slow on mobile." -> score 60. Example: "Something is wrong with login." -> score 50.
- 10-40 (Low): Extremely vague, single-word or generic complaint with no actionable details. Example: "fix it", "help", "system broken". -> score 20-30."""

DUE_DATE_INSTRUCTION = """Today's date is {today}. If the text explicitly states a deadline or due
date (e.g. "due today", "by Friday", "before the 20th", "renewal is due today"), resolve it to an
absolute date in YYYY-MM-DD format using today's date as the reference point. If no deadline is
stated, leave due_date null — do not guess or infer one."""

EMAIL_PROMPT = """You are triaging a user-reported issue from an email.
Extract exactly one issue from the text below. Pick category strictly from: {categories}
""" + CONFIDENCE_CALIBRATION_INSTRUCTION + """
""" + DUE_DATE_INSTRUCTION + """

Email text:
{raw_text}
"""

MEETING_PROMPT = """You are triaging notes from a project discussion meeting.
Extract the single most important/actionable issue mentioned. Pick category strictly from: {categories}
""" + CONFIDENCE_CALIBRATION_INSTRUCTION + """
""" + DUE_DATE_INSTRUCTION + """

Meeting notes:
{raw_text}
"""


def extract_node(state: TextState) -> dict:
    """Extract one structured issue and the model's confidence in that extraction."""
    raw_text = state["raw_text"]
    source_tag = state.get("source_tag", "email")
    emit_log(f"[extract] Reading {source_tag} input ({len(raw_text)} chars).")

    active_categories = get_all_categories()
    prompt = (
        EMAIL_PROMPT if source_tag == "email" else MEETING_PROMPT
    ).format(categories=", ".join(active_categories), raw_text=raw_text, today=date.today().isoformat())

    try:
        if structured_llm is None:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        result = structured_llm.invoke(prompt)
        category = result.category if result.category in active_categories else "other"

        confidence = max(0, min(100, result.confidence))
        due_date, due_date_defaulted = _resolve_due_date(result.due_date, result.urgency)
        emit_log(
            f"[extract] Category: {category} | Urgency: {result.urgency} | "
            f"Confidence: {confidence}%"
        )
        if due_date_defaulted:
            emit_log(
                f"[extract] No due date mentioned in the text; defaulting to {due_date} "
                f"({DUE_DATE_SLA_DAYS.get(result.urgency, DUE_DATE_SLA_DAYS['medium'])}-day SLA "
                f"for '{result.urgency}' urgency).",
                "warning",
            )
        else:
            emit_log(f"[extract] Due date extracted from text: {due_date}")
        return {
            "summary": result.summary,
            "description": result.description,
            "category": category,
            "urgency": result.urgency,
            "confidence": confidence,
            "due_date": due_date,
            "due_date_defaulted": due_date_defaulted,
        }
    except Exception as exc:
        import sys
        print(f"[extract] Exception calling LLM: {exc}", file=sys.stderr)
        emit_log(f"[extract] LLM call failed; using heuristic fallback. ({exc})", "warning")
        text_lower = raw_text.lower()
        if any(word in text_lower for word in ["billing", "payment", "card", "checkout", "subscription", "invoice"]):
            category, summary = "billing", "Payment / Billing Issue Reported"
        elif any(word in text_lower for word in ["infra", "kubernetes", "k8s", "server", "memory", "cpu", "node", "aws", "postgresql", "database", "pool"]):
            category, summary = "infra", "Infrastructure Resource Warning / Outage"
        elif any(word in text_lower for word in ["auth", "login", "credentials", "password", "sign in", "sso", "token"]):
            category, summary = "auth", "Authentication / Login Failure"
        elif any(word in text_lower for word in ["bug", "error", "500", "exception", "crash"]):
            category, summary = "bug", "Software Bug Reported"
        elif any(word in text_lower for word in ["feature", "request", "add", "enhancement"]):
            category, summary = "feature_request", "Feature Request Submitted"
        else:
            category, summary = "other", "Issue Reported"

        urgency = "high" if any(
            word in text_lower
            for word in ["asap", "due today", "urgent", "critical", "peak", "98%", "immediately"]
        ) else "medium"
        lines = [line.strip() for line in raw_text.strip().split("\n") if line.strip()]
        description = lines[-1] if lines else raw_text.strip()
        confidence = 40
        emit_log(
            f"[extract] Heuristic result: {category} | {urgency} | Confidence: {confidence}%",
            "warning",
        )
        # No LLM ran, so there's no reliable way to pull an explicit due date from
        # the text; always fall back to the urgency-based SLA default.
        due_date, due_date_defaulted = _resolve_due_date(None, urgency)
        emit_log(
            f"[extract] No LLM available to read a due date; defaulting to {due_date} "
            f"({DUE_DATE_SLA_DAYS.get(urgency, DUE_DATE_SLA_DAYS['medium'])}-day SLA "
            f"for '{urgency}' urgency).",
            "warning",
        )
        return {
            "summary": summary,
            "description": description,
            "category": category,
            "urgency": urgency,
            "confidence": confidence,
            "due_date": due_date,
            "due_date_defaulted": due_date_defaulted,
        }


def validate_due_date(due_date_str: str | None) -> str | None:
    """Validate a human-supplied due date override.

    Must parse as YYYY-MM-DD and cannot be in the past.
    """
    if not due_date_str or not isinstance(due_date_str, str) or not due_date_str.strip():
        return None
    clean_date = due_date_str.strip()
    try:
        parsed = datetime.strptime(clean_date, "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid due date '{clean_date}'. Expected format YYYY-MM-DD.")
    if parsed < date.today():
        raise ValueError(f"Due date '{clean_date}' cannot be in the past (today is {date.today().isoformat()}).")
    return parsed.isoformat()


def validate_assignee(assignee_str: str | None) -> str | None:
    """Validate a human-supplied assignee override.

    Must be a non-empty string.
    """
    if not assignee_str or not isinstance(assignee_str, str) or not assignee_str.strip():
        return None
    return assignee_str.strip()


def human_review_node(state: TextState, config: RunnableConfig) -> dict:
    """Pause low-confidence runs and resume only after a human decision."""
    confidence = state.get("confidence", 100)
    thread_id = state.get("thread_id") or config.get("configurable", {}).get("thread_id")
    if not thread_id:
        raise ValueError("human_review_node requires a LangGraph thread_id")

    if confidence >= 75:
        emit_log(f"[human_review] Confidence {confidence}% >= 75%. Auto-proceeding.")
        return {"human_decision": "approved"}

    category = state.get("category", "other")
    suggested_assignee = _get_fallback_assignee(category)

    review_item = get_or_create_review_item(
        thread_id=thread_id,
        raw_text=state.get("raw_text", ""),
        source_tag=state.get("source_tag", ""),
        summary=state.get("summary", ""),
        category=category,
        urgency=state.get("urgency", "low"),
        confidence=confidence,
        due_date=state.get("due_date"),
        suggested_assignee=suggested_assignee,
    )

    if review_item.get("status") in {"approved", "rejected"}:
        decision = review_item["status"]
        if decision == "approved":
            emit_log("[human_review] Human approved the extraction. Proceeding.")
        else:
            emit_log("[human_review] Human rejected the extraction. Discarding.", "warning")
        return {"human_decision": decision}

    emit_log(f"[human_review] Confidence {confidence}% < 75%. Pausing for human review.", "warning")
    emit_log(
        f"[human_review] Review item {review_item['id']} is waiting for a human decision.",
        "warning",
    )

    # An interrupted node is replayed when resumed. get_or_create_review_item keeps
    # that replay idempotent; the API supplies one of these values through Command.
    human_input = interrupt(
        {
            "reason": "low_confidence",
            "review_id": review_item["id"],
            "confidence": confidence,
        }
    )

    decision = "approved"
    assignee_override = None
    due_date_override = None

    if isinstance(human_input, dict):
        decision_raw = human_input.get("decision") or human_input.get("status")
        if decision_raw is None and "approved" in human_input:
            decision_raw = "approved" if human_input["approved"] else "rejected"
        if decision_raw:
            decision = str(decision_raw).lower()
        assignee_override = human_input.get("assignee")
        due_date_override = human_input.get("due_date")
    else:
        decision = str(human_input).lower()

    if decision in {"reject", "rejected"}:
        emit_log("[human_review] Human rejected the extraction. Discarding.", "warning")
        return {"human_decision": "rejected"}

    updates: dict = {"human_decision": "approved"}

    if assignee_override:
        valid_assignee = validate_assignee(assignee_override)
        if valid_assignee:
            updates["assignee"] = valid_assignee
            emit_log(f"[human_review] Human overridden assignee: {valid_assignee}")

    if due_date_override:
        valid_due_date = validate_due_date(due_date_override)
        if valid_due_date:
            updates["due_date"] = valid_due_date
            emit_log(f"[human_review] Human overridden due date: {valid_due_date}")

    emit_log("[human_review] Human approved the extraction. Proceeding.")
    return updates


def check_duplicate_node(state: TextState) -> dict:
    """Check JIRA for duplicate tickets."""
    summary = state.get("summary", "")
    category = state.get("category", "other")
    emit_log(f"[check_duplicate] Searching JIRA for duplicates: '{summary}'")
    existing_key = jira_client.search_duplicate(summary=summary, category=category)
    if existing_key:
        emit_log(f"[check_duplicate] Duplicate found: {existing_key}", "warning")
    else:
        emit_log("[check_duplicate] No duplicate found. Proceeding to create ticket.")
    return {"existing_ticket_key": existing_key}


def log_duplicate_node(state: TextState) -> dict:
    """Terminal node for a duplicate branch."""
    existing_key = state.get("existing_ticket_key")
    emit_log(
        f"[log_duplicate] Duplicate ticket {existing_key} already exists. No new ticket created.",
        "warning",
    )
    return {}


def create_ticket_node(state: TextState) -> dict:
    """Create a new JIRA ticket."""
    summary = state.get("summary", "")
    description = state.get("description", "")
    category = state.get("category", "other")
    urgency = state.get("urgency", "low")
    due_date = state.get("due_date")
    emit_log(f"[create_ticket] Creating JIRA ticket: '{summary}' [{category}] [{urgency}] [due: {due_date}]")
    new_key = jira_client.create_ticket(
        summary=summary,
        description=description,
        category=category,
        urgency=urgency,
        due_date=due_date,
    )
    emit_log(f"[create_ticket] Ticket created: {new_key}")
    return {"new_ticket_key": new_key}


def route_assignee_node(state: TextState) -> dict:
    """Smart context-aware routing considering Category candidates, Calendar OOO, and Jira Workload."""
    existing_assignee = state.get("assignee")
    if existing_assignee:
        emit_log(f"[route_assignee] Assignee already set (human override): {existing_assignee}")
        return {}

    category = state.get("category", "other")
    candidates = get_team_members_by_category(category)

    if not candidates:
        fallback_assignee = _get_fallback_assignee(category)
        emit_log(f"[route_assignee] No dynamic candidates in DB for category '{category}'. Falling back to default: {fallback_assignee}")
        return {"assignee": fallback_assignee}

    emit_log(f"[route_assignee] Evaluating {len(candidates)} candidate(s) for category '{category}'...")
    best_candidate = None
    min_workload = float("inf")

    for candidate in candidates:
        name = candidate.get("name")
        email = candidate.get("email")
        jira_id = candidate.get("jira_account_id")

        # 1. Calendar / OOO Check
        if is_user_ooo_today(email):
            emit_log(f"[route_assignee] Candidate {name} ({email}) is Out of Office today. Skipping.", "warning")
            continue

        # 2. Jira Active Ticket Workload Check
        workload = jira_client.get_user_open_ticket_count(jira_id)
        emit_log(f"[route_assignee] Candidate {name} ({email}) has {workload} active Jira ticket(s).")

        if workload < min_workload:
            min_workload = workload
            best_candidate = candidate

    if best_candidate:
        chosen_assignee = best_candidate.get("jira_account_id")
        emit_log(
            f"[route_assignee] Selected {best_candidate.get('name')} ({chosen_assignee}) "
            f"for category '{category}' (Active load: {min_workload} tickets)."
        )
        return {"assignee": chosen_assignee}

    # All candidates OOO fallback
    fallback_assignee = _get_fallback_assignee(category)
    emit_log(
        f"[route_assignee] All candidates for '{category}' are currently OOO. Falling back to default lead: {fallback_assignee}",
        "warning",
    )
    return {"assignee": fallback_assignee}


JUDGE_PROMPT = """You are an expert AI Auditor / Judge evaluating a ticket routing decision.

TICKET DATA & ROUTING DECISION:
- Ticket Key: {ticket_id}
- Summary: {summary}
- Description: {description}
- Category: {category}
- Urgency / Priority: {urgency}
- Assigned Lead: {assignee}
- Due Date: {due_date} (Defaulted by SLA: {due_date_defaulted})
- Human Review Decision: {human_decision}
- Original Raw Text:
{raw_text}

EVALUATION RUBRIC:
1. Assignee Suitability: Does the assigned lead plausibly fit the ticket's category and required domain expertise?
2. Due Date Reasonableness: Is the assigned due date realistic and appropriate for the stated priority / urgency?
3. Text Consistency: Does anything in the ticket routing (assignee, category, urgency, due date) contradict the raw issue text?

Provide a score (0 to 100), detailed reasoning, and a list of specific warning flags if any issues exist.
"""


def _run_judge_background(ticket_id: str, state: TextState) -> None:
    """Non-blocking background worker that calls the LLM judge and persists the verdict."""
    emit_log(f"[judge] Starting async evaluation for {ticket_id}...")
    try:
        if judge_structured_llm is None:
            emit_log(f"[judge] LLM unavailable; skipping judge evaluation for {ticket_id}.", "warning")
            return

        raw_text = state.get("raw_text", "")
        summary = state.get("summary", "")
        description = state.get("description", "")
        category = state.get("category", "other")
        urgency = state.get("urgency", "low")
        assignee = state.get("assignee", "unassigned")
        due_date = state.get("due_date", "None")
        due_date_defaulted = state.get("due_date_defaulted", False)
        human_decision = state.get("human_decision", "none")
        was_human_approved = human_decision == "approved"

        prompt = JUDGE_PROMPT.format(
            ticket_id=ticket_id,
            summary=summary,
            description=description,
            category=category,
            urgency=urgency,
            assignee=assignee,
            due_date=due_date,
            due_date_defaulted=due_date_defaulted,
            human_decision=human_decision,
            raw_text=raw_text,
        )

        verdict: JudgeVerdict = judge_structured_llm.invoke(prompt)

        snapshot = {
            "summary": summary,
            "category": category,
            "urgency": urgency,
            "assignee": assignee,
            "due_date": due_date,
            "raw_text": raw_text[:300],
        }

        save_judge_verdict(
            ticket_id=ticket_id,
            score=max(0, min(100, verdict.score)),
            reasoning=verdict.reasoning,
            flags=verdict.flags,
            was_human_approved=was_human_approved,
            decision_snapshot=snapshot,
        )
        emit_log(
            f"[judge] Verdict saved for {ticket_id}: Score {verdict.score}/100. "
            f"Flags: {len(verdict.flags)} item(s)."
        )
    except Exception as exc:
        import sys
        print(f"[judge] Exception calling Judge LLM for {ticket_id}: {exc}", file=sys.stderr)
        emit_log(f"[judge] Evaluation failed/timed out for {ticket_id}: {exc}", "warning")


def update_ticket_node(state: TextState) -> dict:
    """Assign a created JIRA ticket to its owner."""
    new_ticket_key = state.get("new_ticket_key")
    assignee = state.get("assignee")
    if new_ticket_key and assignee:
        emit_log(f"[update_ticket] Assigning {new_ticket_key} to {assignee}.")
        jira_client.update_assignee(new_ticket_key, assignee)
        emit_log("[update_ticket] Assignment complete.")

        # Non-blocking background judge evaluation
        thread = threading.Thread(
            target=_run_judge_background,
            args=(new_ticket_key, state),
            daemon=True,
        )
        thread.start()
    return {}