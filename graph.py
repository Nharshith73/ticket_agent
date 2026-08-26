import sqlite3

from langgraph.graph import END, START, StateGraph
try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:
    SqliteSaver = None

try:
    from langgraph.checkpoint.memory import MemorySaver
except ImportError:
    MemorySaver = None

from database import DB_PATH
from nodes import (
    check_duplicate_node,
    create_ticket_node,
    extract_node,
    human_review_node,
    log_duplicate_node,
    route_assignee_node,
    update_ticket_node,
)
from state import TextState


def route_after_human_review(state: TextState) -> str:
    """Rejected reviews end without calling JIRA; all approvals continue."""
    return "check_duplicate" if state.get("human_decision") == "approved" else "end"


def route_after_check_duplicate(state: TextState) -> str:
    """Choose the duplicate terminal branch or the create-and-assign branch."""
    return "log_duplicate" if state.get("existing_ticket_key") is not None else "create_ticket"


builder = StateGraph(TextState)
builder.add_node("extract", extract_node)
builder.add_node("human_review", human_review_node)
builder.add_node("check_duplicate", check_duplicate_node)
builder.add_node("log_duplicate", log_duplicate_node)
builder.add_node("create_ticket", create_ticket_node)
builder.add_node("route_assignee", route_assignee_node)
builder.add_node("update_ticket", update_ticket_node)

builder.add_edge(START, "extract")
builder.add_edge("extract", "human_review")
builder.add_conditional_edges(
    "human_review",
    route_after_human_review,
    {"check_duplicate": "check_duplicate", "end": END},
)
builder.add_conditional_edges(
    "check_duplicate",
    route_after_check_duplicate,
    {"log_duplicate": "log_duplicate", "create_ticket": "create_ticket"},
)
builder.add_edge("log_duplicate", END)
builder.add_edge("create_ticket", "route_assignee")
builder.add_edge("route_assignee", "update_ticket")
builder.add_edge("update_ticket", END)

# SqliteSaver with MemorySaver fallback for serverless safety
checkpointer = None
if SqliteSaver:
    try:
        checkpoint_connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        checkpointer = SqliteSaver(checkpoint_connection)
    except Exception:
        pass

if checkpointer is None and MemorySaver:
    try:
        checkpointer = MemorySaver()
    except Exception:
        pass

app = builder.compile(checkpointer=checkpointer)
