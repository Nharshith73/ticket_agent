"""FastAPI dashboard for live triage logs and human review decisions."""

import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from database import (
    add_team_member,
    delete_team_member,
    get_all_team_members,
    get_judge_verdict,
    get_member_availability,
    get_pending_reviews,
    get_recent_logs,
    get_review_by_id,
    get_review_by_thread_id,
    reset_review_status_to_pending,
    set_member_availability,
    set_review_status_if_pending,
)
from graph import app as graph_app
from jira_client import JiraClient
from log_utils import emit_log, stream_log_events

ADMIN_TEMPLATE_PATH = Path(__file__).with_name("templates") / "admin.html"
jira_service = JiraClient()


TEMPLATE_PATH = Path(__file__).with_name("templates") / "index.html"
app = FastAPI(title="Support Ticket Triage Dashboard")


from typing import Optional
from nodes import validate_assignee, validate_due_date


class IngestRequest(BaseModel):
    raw_text: str = Field(min_length=1, max_length=50_000)
    source_tag: str = Field(default="email", pattern="^(email|meeting_note)$")


class ApproveRequest(BaseModel):
    assignee: Optional[str] = None
    due_date: Optional[str] = None


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _initial_state(raw_text: str, source_tag: str, thread_id: str) -> dict:
    return {
        "thread_id": thread_id,
        "raw_text": raw_text,
        "source_tag": source_tag,
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


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(TEMPLATE_PATH, media_type="text/html")


@app.get("/stream/logs")
async def stream_logs(request: Request) -> StreamingResponse:
    """Publish node logs as Server-Sent Events without polling."""

    async def events():
        async for entry in stream_log_events():
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(entry)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/api/logs")
async def get_logs() -> list[dict]:
    """Return recent historical logs."""
    return get_recent_logs(100)


@app.get("/api/review-queue")
async def review_queue() -> list[dict]:
    return get_pending_reviews()


async def _apply_review_decision(
    item_id: str, decision: str, request_data: Optional[ApproveRequest] = None
) -> dict:
    review_item = get_review_by_id(item_id)
    if review_item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review item not found")

    resume_payload: dict | str = decision
    if decision == "approved" and request_data:
        assignee_val = request_data.assignee.strip() if request_data.assignee else None
        due_date_val = request_data.due_date.strip() if request_data.due_date else None

        try:
            if due_date_val:
                due_date_val = validate_due_date(due_date_val)
            if assignee_val:
                assignee_val = validate_assignee(assignee_val)
        except ValueError as val_err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(val_err),
            ) from val_err

        resume_payload = {
            "decision": "approved",
            "assignee": assignee_val,
            "due_date": due_date_val,
        }

    if not set_review_status_if_pending(item_id, decision):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review item has already been decided",
        )

    emit_log(f"[review] Human {decision} review item {item_id}.")
    try:
        await run_in_threadpool(
            graph_app.invoke,
            Command(resume=resume_payload),
            _config(review_item["thread_id"]),
        )
    except Exception as exc:
        reset_review_status_to_pending(item_id, decision)
        emit_log(f"[review] Could not resume {item_id}: {exc}", "error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The graph could not resume; the item was returned to the review queue",
        ) from exc

    emit_log(f"[review] Graph run {review_item['thread_id']} completed after {decision}.")
    return {
        "id": item_id,
        "thread_id": review_item["thread_id"],
        "status": decision,
    }


@app.post("/api/review/{item_id}/approve")
async def approve_review(
    item_id: str, request: Optional[ApproveRequest] = None
) -> dict:
    return await _apply_review_decision(item_id, "approved", request)


@app.post("/api/review/{item_id}/reject")
async def reject_review(item_id: str) -> dict:
    return await _apply_review_decision(item_id, "rejected")


@app.post("/api/ingest")
async def ingest(request: IngestRequest) -> dict:
    """Start a durable graph run from text submitted by the dashboard."""
    raw_text = request.raw_text.strip()
    if not raw_text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="raw_text is required")

    thread_id = str(uuid4())
    emit_log(f"[ingest] Starting {request.source_tag} triage run {thread_id}.")
    try:
        await run_in_threadpool(
            graph_app.invoke,
            _initial_state(raw_text, request.source_tag, thread_id),
            _config(thread_id),
        )
    except Exception as exc:
        emit_log(f"[ingest] Graph run {thread_id} failed: {exc}", "error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The triage run failed; check the live log panel",
        ) from exc

    review_item = get_review_by_thread_id(thread_id)
    workflow_status = "pending_review" if review_item and review_item["status"] == "pending" else "completed"
    emit_log(f"[ingest] Graph run {thread_id} is {workflow_status}.")
    return {
        "thread_id": thread_id,
        "status": workflow_status,
        "review_id": review_item["id"] if workflow_status == "pending_review" else None,
    }


@app.get("/admin", include_in_schema=False)
async def admin_dashboard() -> FileResponse:
    if not ADMIN_TEMPLATE_PATH.exists():
        raise HTTPException(status_code=status.HTTP_444_NOT_FOUND if hasattr(status, 'HTTP_444_NOT_FOUND') else 404, detail="Admin template not found")
    return FileResponse(ADMIN_TEMPLATE_PATH, media_type="text/html")


class TeamMemberCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    primary_category: str = Field(min_length=1)
    jira_account_id: Optional[str] = None


class AvailabilitySetRequest(BaseModel):
    email: str
    status: str = Field(pattern="^(available|ooo|vacation)$")
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/team")
async def list_team() -> list[dict]:
    """List all team members with availability and live Jira ticket counts."""
    members = get_all_team_members()
    result = []
    for member in members:
        email = member.get("email", "")
        jira_id = member.get("jira_account_id", "")
        avail = get_member_availability(email)
        open_tickets = jira_service.get_user_open_ticket_count(jira_id)
        
        item = dict(member)
        item["availability"] = avail.get("status") if avail else "available"
        item["availability_record"] = avail
        item["open_ticket_count"] = open_tickets
        result.append(item)
    return result


@app.post("/api/team")
async def create_or_update_member(req: TeamMemberCreateRequest) -> dict:
    """Add/update a team member, automatically fetching Jira account ID by email if not provided."""
    name = req.name.strip()
    email = req.email.strip().lower()
    category = req.primary_category.strip().lower()
    
    jira_id = req.jira_account_id.strip() if req.jira_account_id else None
    if not jira_id:
        # Auto-lookup in Jira Cloud API by email
        jira_id = jira_service.get_account_id_by_email(email)
    
    member = add_team_member(
        name=name,
        email=email,
        primary_category=category,
        jira_account_id=jira_id,
    )
    emit_log(f"[admin] Saved team member {name} ({email}) for category '{category}' linked to Jira ID: {jira_id}")
    return member


@app.delete("/api/team/{member_id}")
async def remove_member(member_id: str) -> dict:
    """Remove a team member by ID."""
    success = delete_team_member(member_id)
    if not success:
        raise HTTPException(status_code=404, detail="Team member not found")
    emit_log(f"[admin] Deleted team member {member_id}.")
    return {"success": True, "id": member_id}


@app.post("/api/availability")
async def update_availability(req: AvailabilitySetRequest) -> dict:
    """Set member availability status (available, ooo, vacation)."""
    record = set_member_availability(
        email=req.email,
        status=req.status,
        start_date=req.start_date,
        end_date=req.end_date,
        notes=req.notes,
    )
    emit_log(f"[admin] Updated availability for {req.email}: {req.status}")
    return record


@app.get("/api/verdict/{ticket_id}")
async def fetch_verdict(ticket_id: str) -> dict:
    """Fetch the LLM-as-a-judge evaluation verdict for a given ticket ID."""
    clean_key = ticket_id.strip()
    verdict = get_judge_verdict(clean_key)
    if not verdict:
        return {"found": False, "ticket_id": clean_key, "message": "No judge verdict recorded yet."}
    return {"found": True, "verdict": verdict}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)


