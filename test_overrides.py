import sys
from datetime import date, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from database import get_or_create_review_item, get_review_by_thread_id
from graph import app
from langgraph.types import Command
from nodes import validate_assignee, validate_due_date
from server import app as fastapi_app


def test_validation_helpers():
    print("[TEST] Testing validation helpers...")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Due date validations
    assert validate_due_date(tomorrow) == tomorrow
    assert validate_due_date(None) is None
    assert validate_due_date("  ") is None

    try:
        validate_due_date(yesterday)
        assert False, "Should have raised ValueError for past date"
    except ValueError as e:
        assert "cannot be in the past" in str(e)

    try:
        validate_due_date("invalid-format")
        assert False, "Should have raised ValueError for malformed date"
    except ValueError as e:
        assert "Expected format YYYY-MM-DD" in str(e)

    # Assignee validations
    assert validate_assignee(" user_acc_test ") == "user_acc_test"
    assert validate_assignee(None) is None
    assert validate_assignee("  ") is None
    print("[PASS] Validation helpers verified.")


def test_graph_override_flow():
    print("[TEST] Testing graph execution flow with overrides...")
    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    # Low-confidence text to trigger interrupt pause
    initial_state = {
        "thread_id": thread_id,
        "raw_text": "help fix system",
        "source_tag": "email",
        "category": "",
        "summary": "",
        "description": "",
        "urgency": "",
        "confidence": 0,
        "human_decision": "",
        "existing_ticket_key": None,
        "new_ticket_key": None,
        "assignee": None,
    }

    state_1 = app.invoke(initial_state, config)
    snapshot = app.get_state(config)
    assert snapshot.next == ("human_review",), f"Expected pause at human_review, got {snapshot.next}"

    future_date = (date.today() + timedelta(days=5)).isoformat()
    custom_assignee = "override_team_lead_123"

    # Resume with overrides
    resume_payload = {
        "decision": "approved",
        "assignee": custom_assignee,
        "due_date": future_date,
    }

    final_state = app.invoke(Command(resume=resume_payload), config)

    assert final_state.get("human_decision") == "approved"
    assert final_state.get("assignee") == custom_assignee, f"Expected {custom_assignee}, got {final_state.get('assignee')}"
    assert final_state.get("due_date") == future_date, f"Expected {future_date}, got {final_state.get('due_date')}"
    print("[PASS] Graph override flow verified.")


def test_fastapi_approve_endpoint_overrides():
    print("[TEST] Testing FastAPI approve endpoint with overrides...")
    client = TestClient(fastapi_app)

    # Ingest low-confidence issue
    res = client.post("/api/ingest", json={"raw_text": "fix something broken", "source_tag": "email"})
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "pending_review"
    review_id = data.get("review_id")
    assert review_id is not None

    future_date = (date.today() + timedelta(days=10)).isoformat()
    past_date = (date.today() - timedelta(days=2)).isoformat()

    # 1. Test invalid due date returns 422
    err_res = client.post(f"/api/review/{review_id}/approve", json={"due_date": past_date})
    assert err_res.status_code == 422
    assert "cannot be in the past" in err_res.json()["detail"]

    # 2. Test valid approval with overrides
    approve_res = client.post(
        f"/api/review/{review_id}/approve",
        json={"assignee": "api_override_lead", "due_date": future_date},
    )
    assert approve_res.status_code == 200
    assert approve_res.json()["status"] == "approved"
    print("[PASS] FastAPI endpoint overrides verified.")


if __name__ == "__main__":
    test_validation_helpers()
    test_graph_override_flow()
    test_fastapi_approve_endpoint_overrides()
    print("\nALL OVERRIDE TESTS PASSED SUCCESSFULLY!")
