from typing import TypedDict, Optional, Literal
from pydantic import BaseModel, Field

from database import get_all_categories

def get_active_categories() -> list[str]:
    return get_all_categories()

CATEGORIES = get_all_categories()

class TextState(TypedDict):
    thread_id: str                       # identifies this durable LangGraph run
    raw_text: str                        # real value at invoke time, overwritten each run
    source_tag: str                      # "email" or "meeting_note"
    category: str                        # filled by extract_node
    summary: str                         # filled by extract_node
    description: str                     # filled by extract_node
    urgency: str                         # filled by extract_node
    confidence: int                      # filled by extract_node (0-100)
    due_date: Optional[str]              # filled by extract_node (YYYY-MM-DD)
    due_date_defaulted: Optional[bool]   # filled by extract_node
    human_decision: str                  # filled by human_review_node ("approved"/"rejected"/"")
    existing_ticket_key: Optional[str]   # filled by check_duplicate_node
    new_ticket_key: Optional[str]        # filled by create_ticket_node
    assignee: Optional[str]              # filled by route_assignee_node

class ExtractedIssue(BaseModel):
    summary: str = Field(description="One-line issue title")
    description: str = Field(description="Full issue detail")
    category: str = Field(description="The primary domain / category of the issue (e.g. frontend, backend, design, marketing, auth, billing, infra, bug, feature_request, other)")
    urgency: Literal["low", "medium", "high"]
    confidence: int = Field(
        description="Your confidence score (0-100) on how accurately this issue was extracted from the text. "
                    "100 = perfectly clear issue, 50 = somewhat ambiguous, 0 = cannot determine.",
        ge=0, le=100
    )
    due_date: Optional[str] = Field(
        default=None,
        description="Due date in YYYY-MM-DD format if explicitly stated in raw text, otherwise null."
    )


class JudgeVerdict(BaseModel):
    score: int = Field(description="Evaluation score between 0 and 100", ge=0, le=100)
    reasoning: str = Field(description="Detailed explanation evaluating assignee fit, due date reasonableness, and contradictions")
    flags: list[str] = Field(default_factory=list, description="List of potential concerns, discrepancies, or issues identified")

