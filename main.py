import argparse
import sys
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv()

from database import get_review_by_thread_id, set_review_status_if_pending
from graph import app
from langgraph.types import Command
import nodes


def run_triage(raw_text: str, source_tag: str = "email", auto_approve: bool = False):
    print("=" * 70)
    print(f"RUNNING TRIAGE FOR SOURCE: [{source_tag.upper()}]")
    print(f"Raw Input: {raw_text.strip()}")
    print("-" * 70)

    thread_id = str(uuid4())
    initial_state = {
        "thread_id": thread_id,
        "raw_text": raw_text,
        "source_tag": source_tag,
        "category": "",
        "summary": "",
        "description": "",
        "urgency": "",
        "confidence": 0,
        "due_date": None,
        "due_date_defaulted": None,
        "human_decision": "",
        "existing_ticket_key": None,
        "new_ticket_key": None,
        "assignee": None,
    }

    config = {"configurable": {"thread_id": thread_id}}
    final_state = app.invoke(initial_state, config)

    snapshot = app.get_state(config)
    if snapshot.next:
        review_item = get_review_by_thread_id(thread_id)
        confidence = final_state.get("confidence", 0)
        print(f"\n[cli] Run paused at human review (confidence: {confidence}%).")

        assignee_override = None
        due_date_override = None

        if auto_approve:
            decision = "approved"
            print("[cli] Auto-approving review item (--demo / auto mode)...")
        else:
            prompt_msg = f"Extraction confidence is low ({confidence}%). Do you approve JIRA ticket creation? (y/N): "
            user_choice = input(prompt_msg).strip().lower()
            decision = "approved" if user_choice in ("y", "yes") else "rejected"

            if decision == "approved":
                raw_assignee = input("Assignee override [Press Enter to keep default]: ").strip()
                if raw_assignee:
                    assignee_override = raw_assignee
                raw_due_date = input("Due date override (YYYY-MM-DD) [Press Enter to keep default]: ").strip()
                if raw_due_date:
                    due_date_override = raw_due_date

        if review_item:
            set_review_status_if_pending(review_item["id"], decision)

        resume_payload: dict | str = (
            {
                "decision": decision,
                "assignee": assignee_override,
                "due_date": due_date_override,
            }
            if decision == "approved"
            else decision
        )

        final_state = app.invoke(Command(resume=resume_payload), config)

    print("-" * 70)
    print("FINAL STATE RESULT:")
    for key, value in final_state.items():
        print(f"  {key}: {value}")
    print("=" * 70 + "\n")
    return final_state


def run_demo_suite():
    print("=" * 70)
    print("RUNNING DEMO REGRESSION SUITE (--demo)")
    print("=" * 70 + "\n")

    # Sample 1: Email report (Billing issue)
    sample_email = """
    From: customer@example.com
    Subject: Unable to complete payment during checkout

    Hi support team,
    Whenever I try to update my subscription card details on the billing page, the page hangs and returns
    a 500 error. Please help ASAP as our account renewal is due today.
    """
    run_triage(sample_email, source_tag="email", auto_approve=True)

    # Sample 2: Meeting note (Infra issue)
    sample_meeting_note = """
    Team Sync Notes (2026-08-06):
    - Alex noted that EU-West region Kubernetes nodes hit 98% memory consumption during peak hours yesterday.
    - We need to scale the node pool and adjust auto-scaling thresholds before the weekend.
    - Sarah presented updated wireframes for user profile page.
    """
    run_triage(sample_meeting_note, source_tag="meeting_note", auto_approve=True)

    # Sample 3: Duplicate detection test (Simulated duplicate match)
    print("TESTING CONDITIONAL FORK (DUPLICATE FOUND)...")
    original_search = nodes.jira_client.search_duplicate
    nodes.jira_client.search_duplicate = lambda summary, category: "EXISTING-123"

    sample_duplicate_email = """
    From: user2@example.com
    Subject: Login failure on mobile app

    Getting invalid credentials error every time I try to log in on iOS.
    """
    run_triage(sample_duplicate_email, source_tag="email", auto_approve=True)

    nodes.jira_client.search_duplicate = original_search


def main():
    parser = argparse.ArgumentParser(
        description="Run LangGraph Support-Ticket Triage agent on custom text or files."
    )
    parser.add_argument(
        "--text",
        "-t",
        type=str,
        help="Raw text content to triage directly.",
    )
    parser.add_argument(
        "--file",
        "-f",
        type=str,
        help="Path to a text file containing issue report or meeting notes to triage.",
    )
    parser.add_argument(
        "--source",
        "-s",
        type=str,
        choices=["email", "meeting_note"],
        default="email",
        help="Source type tag ('email' or 'meeting_note'). Default: email",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run hardcoded 3-sample regression test suite (opt-in only).",
    )

    args = parser.parse_args()

    if args.demo:
        run_demo_suite()
        return

    raw_text = ""
    if args.text:
        raw_text = args.text.strip()
    elif args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        with open(file_path, "r", encoding="utf-8") as f:
            raw_text = f.read().strip()
    else:
        print("No --text, --file, or --demo arguments provided.")
        print("Please enter/paste your text below (press Enter then Ctrl+Z / Ctrl+D or type 'END' on a new line to finish):\n")
        lines = []
        try:
            while True:
                line = input()
                if line.strip() == "END":
                    break
                lines.append(line)
        except EOFError:
            pass
        raw_text = "\n".join(lines).strip()

    if not raw_text:
        print("Error: No text provided to triage.", file=sys.stderr)
        sys.exit(1)

    run_triage(raw_text, source_tag=args.source)


if __name__ == "__main__":
    main()
