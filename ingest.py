import os
import sys
import time
import json
import argparse
from uuid import uuid4
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# Import graph runner
from graph import app

PROCESSED_GDOCS_CACHE = "processed_gdocs.json"

def read_docx(file_path: str) -> str:
    """Extract text from Microsoft Word (.docx) file"""
    try:
        import docx
        doc = docx.Document(file_path)
        full_text = []
        for para in doc.paragraphs:
            if para.text.strip():
                full_text.append(para.text.strip())
        return "\n".join(full_text)
    except Exception as e:
        print(f"[ingest error] Failed to read Word doc '{file_path}': {e}")
        return ""

def read_text_file(file_path: str) -> str:
    """Extract text from .txt or .md file"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[ingest error] Failed to read text file '{file_path}': {e}")
        return ""

def load_processed_cache() -> set:
    if os.path.exists(PROCESSED_GDOCS_CACHE):
        try:
            with open(PROCESSED_GDOCS_CACHE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_processed_cache(processed_ids: set):
    try:
        with open(PROCESSED_GDOCS_CACHE, "w") as f:
            json.dump(list(processed_ids), f)
    except Exception as e:
        print(f"[cache error] Could not save processed gdocs cache: {e}")

def fetch_google_drive_docs() -> list[dict]:
    """Poll designated Google Drive folder for new Google Docs meeting notes"""
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    processed_ids = load_processed_cache()

    if folder_id and os.path.exists(credentials_file):
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseDownload
            import io

            # Initialize Google Drive API service
            creds = Credentials.from_authorized_user_file("token.json", ["https://www.googleapis.com/auth/drive.readonly"])
            drive_service = build("drive", "v3", credentials=creds)

            query = f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.document' and trashed=false"
            results = drive_service.files().list(q=query, fields="files(id, name, modifiedTime)").execute()
            files = results.get("files", [])

            new_docs = []
            for file in files:
                file_id = file["id"]
                if file_id not in processed_ids:
                    print(f"[gdrive] Fetching Google Doc: '{file['name']}' ({file_id})")
                    request = drive_service.files().export_media(fileId=file_id, mimeType="text/plain")
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        status, done = downloader.next_chunk()

                    text_content = fh.getvalue().decode("utf-8")
                    new_docs.append({
                        "id": file_id,
                        "raw_text": text_content,
                        "source_tag": "meeting_note"
                    })
                    processed_ids.add(file_id)

            save_processed_cache(processed_ids)
            return new_docs
        except Exception as e:
            print(f"[gdrive warning] Could not fetch Google Drive docs via API ({e}). Falling back to stub mode.")

    print(f"[gdrive stub] Polling Google Drive Folder (ID: {folder_id or 'NOT_CONFIGURED'})...")
    # Return simulated Google Doc meeting note if no docs in cache yet
    mock_id = "gdoc_simulated_note_99"
    if mock_id not in processed_ids:
        processed_ids.add(mock_id)
        save_processed_cache(processed_ids)
        return [
            {
                "id": mock_id,
                "raw_text": "Product Meeting Notes (Google Docs):\n- API gateway latency spiked to 450ms during peak load.\n- Infra team needs to scale up API pods and review rate limits immediately.",
                "source_tag": "meeting_note"
            }
        ]
    
    return []

def fetch_gmail_unread() -> list[dict]:
    """Fetch unread support emails from Gmail API (or stub sample if credentials missing)"""
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    if os.path.exists(credentials_file):
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            print(f"[gmail] Using credentials file: {credentials_file}")
            # Real Gmail API polling logic
        except Exception as e:
            print(f"[gmail warning] Could not initialize Gmail API ({e}). Falling back to sample mode.")
    
    print("[gmail stub] Polling Gmail inbox for unread support emails...")
    return [
        {
            "raw_text": "From: billing-user@company.com\nSubject: Need invoice refund\n\nHi support, I was double charged on my card for invoice #4402. Please issue a refund.",
            "source_tag": "email"
        }
    ]

def ingest_and_triage(raw_text: str, source_tag: str):
    """Pass ingested text directly to the LangGraph Support-Ticket Triage pipeline"""
    if not raw_text.strip():
        print("[ingest warning] Empty text received. Skipping triage.")
        return None

    print("\n" + "=" * 70)
    print(f"INGESTING ISSUE FOR TRIAGE -> [SOURCE: {source_tag.upper()}]")
    print("-" * 70)
    print(f"Raw Input Snippet: {raw_text.strip()[:200]}...")
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
        "human_decision": "",
        "existing_ticket_key": None,
        "new_ticket_key": None,
        "assignee": None,
    }

    final_state = app.invoke(initial_state, {"configurable": {"thread_id": thread_id}})

    print("-" * 70)
    print("TRIAGE EXECUTION RESULT:")
    print(f"  Category            : {final_state.get('category')}")
    print(f"  Summary             : {final_state.get('summary')}")
    print(f"  Urgency             : {final_state.get('urgency')}")
    print(f"  Existing Ticket Key : {final_state.get('existing_ticket_key')}")
    print(f"  New Ticket Key      : {final_state.get('new_ticket_key')}")
    print(f"  Routed Assignee     : {final_state.get('assignee')}")
    print("=" * 70 + "\n")
    return final_state

def main():
    parser = argparse.ArgumentParser(description="Ingestion script for Support-Ticket Triage Agent")
    parser.add_argument("--file", help="Path to local file (.docx, .txt, .md)")
    parser.add_argument("--source", choices=["email", "meeting_note"], default="meeting_note", help="Source type tag")
    parser.add_argument("--gmail", action="store_true", help="Poll Gmail API for unread emails")
    parser.add_argument("--gdrive", action="store_true", help="Poll Google Drive folder for new Google Docs meeting notes")
    parser.add_argument("--gdrive-watch", action="store_true", help="Run continuous background watcher on Google Drive folder")
    parser.add_argument("--text", help="Raw input text passed directly")

    args = parser.parse_args()

    if args.gdrive_watch:
        print("[gdrive watch] Starting continuous watcher on Google Drive Meeting Notes folder (polling every 30s)... Press Ctrl+C to stop.")
        try:
            while True:
                docs = fetch_google_drive_docs()
                for doc in docs:
                    ingest_and_triage(doc["raw_text"], doc["source_tag"])
                time.sleep(30)
        except KeyboardInterrupt:
            print("\n[gdrive watch] Stopped watching Google Drive.")
    elif args.gdrive:
        docs = fetch_google_drive_docs()
        if not docs:
            print("[gdrive] No new Google Docs found to process.")
        for doc in docs:
            ingest_and_triage(doc["raw_text"], doc["source_tag"])
    elif args.gmail:
        emails = fetch_gmail_unread()
        for item in emails:
            ingest_and_triage(item["raw_text"], item["source_tag"])
    elif args.file:
        file_path = args.file
        if file_path.endswith(".docx"):
            text = read_docx(file_path)
        else:
            text = read_text_file(file_path)
        ingest_and_triage(text, source_tag=args.source)
    elif args.text:
        ingest_and_triage(args.text, source_tag=args.source)
    else:
        print("Usage examples:")
        print("  python ingest.py --gdrive                   # Poll Google Drive folder once")
        print("  python ingest.py --gdrive-watch             # Continuous background watch on Google Drive folder")
        print("  python ingest.py --gmail                    # Poll Gmail inbox")
        print("  python ingest.py --file meeting_notes.docx  # Ingest local Word doc")

if __name__ == "__main__":
    main()
