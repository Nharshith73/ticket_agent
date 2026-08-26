import os
import base64
import requests
from typing import Optional
from dotenv import load_dotenv

from database import get_jira_config

load_dotenv()

class JiraClient:
    def __init__(self):
        self.reload()

    def reload(self):
        """Reload credentials dynamically from database or .env fallback."""
        db_config = None
        try:
            db_config = get_jira_config()
        except Exception:
            pass

        if db_config:
            self.jira_url = db_config.get("jira_url", "").strip().rstrip("/")
            self.user_email = db_config.get("user_email", "").strip()
            self.api_token = db_config.get("api_token", "").strip()
            self.project_key = db_config.get("project_key", "FA").strip()
        else:
            self.jira_url = os.getenv("JIRA_URL", "").strip().rstrip("/")
            self.user_email = os.getenv("JIRA_USER_EMAIL", "").strip()
            self.api_token = os.getenv("JIRA_API_TOKEN", "").strip()
            self.project_key = os.getenv("JIRA_PROJECT_KEY", "FA").strip()

        self.is_configured = bool(self.jira_url and self.user_email and self.api_token and "example.com" not in self.user_email and "yourdomain.com" not in self.user_email)
        self.my_account_id = None
        if self.is_configured:
            auth_str = f"{self.user_email}:{self.api_token}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            self.headers = {
                "Authorization": f"Basic {b64_auth}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            # Resolve current user's account ID for smart fallback
            try:
                res = requests.get(f"{self.jira_url}/rest/api/3/myself", headers=self.headers, timeout=5)
                if res.status_code == 200:
                    self.my_account_id = res.json().get("accountId")
            except Exception:
                pass
        else:
            self.headers = {}

    def search_duplicate(self, summary: str, category: str) -> Optional[str]:
        """Search JIRA for existing duplicate tickets"""
        self.reload()
        if not self.is_configured:
            print(f"[stub] Would search JIRA for duplicate: category={category}, summary='{summary}'")
            return None

        clean_summary = summary.replace('"', '\\"').strip()
        if not clean_summary:
            return None

        try:
            # JQL search using summary keyword match within project via POST /rest/api/3/search/jql
            url = f"{self.jira_url}/rest/api/3/search/jql"
            payload = {
                "jql": f'project = "{self.project_key}" AND summary ~ "{clean_summary}"'
            }
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                issues = data.get("issues") or data.get("values") or data.get("results") or []
                if issues and isinstance(issues, list):
                    for issue in issues:
                        if isinstance(issue, dict):
                            fields = issue.get("fields", {})
                            existing_summary = fields.get("summary", "")
                            if clean_summary.lower() in existing_summary.lower() or existing_summary.lower() in clean_summary.lower():
                                existing_key = issue.get("key") or issue.get("issueKey")
                                if existing_key:
                                    print(f"[jira] Found existing duplicate ticket: {existing_key}")
                                    return existing_key
            else:
                print(f"[jira warning] JIRA search returned status {response.status_code}: {response.text}")
        except Exception as e:
            print(f"[jira error] Error searching JIRA: {e}")

        return None

    def create_ticket(self, summary: str, description: str, category: str, urgency: str, due_date: Optional[str] = None) -> str:
        """Create a new JIRA ticket using Atlassian Document Format (ADF)"""
        self.reload()
        if not self.is_configured:
            fake_key = f"{self.project_key}-101"
            print(f"[stub] Would create JIRA ticket: key={fake_key}, summary='{summary}', category={category}, urgency={urgency}, due_date={due_date}")
            return fake_key

        url = f"{self.jira_url}/rest/api/3/issue"
        fields_data = {
            "project": {"key": self.project_key},
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Category: {category} | Urgency: {urgency}\n\n{description}"
                            }
                        ]
                    }
                ]
            },
            "issuetype": {"name": "Task"}
        }
        if due_date:
            fields_data["duedate"] = due_date

        payload = {"fields": fields_data}

        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            if response.status_code in (200, 201):
                data = response.json()
                ticket_key = data.get("key", "UNKNOWN-KEY")
                print(f"[jira] Successfully created JIRA ticket: {ticket_key}")
                return ticket_key
            else:
                print(f"[jira error] Failed to create ticket. Status {response.status_code}: {response.text}")
                return f"{self.project_key}-FALLBACK"
        except Exception as e:
            print(f"[jira error] Exception creating ticket: {e}")
            return f"{self.project_key}-FALLBACK"

    def update_assignee(self, ticket_key: str, assignee: str) -> bool:
        """Assign JIRA ticket to account ID / user"""
        self.reload()
        if not self.is_configured:
            print(f"[stub] Would assign JIRA ticket '{ticket_key}' to assignee '{assignee}'")
            return True

        target_assignee = assignee
        if ("user_acc_" in assignee or "account-id-for-" in assignee or not assignee) and self.my_account_id:
            target_assignee = self.my_account_id
            print(f"[jira] Placeholder assignee detected. Automatically assigning to your account ID ({self.my_account_id})")

        url = f"{self.jira_url}/rest/api/3/issue/{ticket_key}/assignee"
        payload = {"accountId": target_assignee}

        try:
            response = requests.put(url, json=payload, headers=self.headers, timeout=10)
            if response.status_code == 204:
                print(f"[jira] Successfully assigned ticket '{ticket_key}' to '{target_assignee}'")
                return True
            elif self.my_account_id and target_assignee != self.my_account_id:
                print(f"[jira warning] Assignment to '{target_assignee}' failed ({response.status_code}). Retrying with your account ID ({self.my_account_id})...")
                fallback_res = requests.put(url, json={"accountId": self.my_account_id}, headers=self.headers, timeout=10)
                if fallback_res.status_code == 204:
                    print(f"[jira] Successfully assigned ticket '{ticket_key}' to fallback account ID ({self.my_account_id})")
                    return True
                else:
                    print(f"[jira warning] Fallback assignment also failed for {ticket_key}. Status {fallback_res.status_code}: {fallback_res.text}")
                    return False
            else:
                print(f"[jira warning] Assignment failed for {ticket_key}. Status {response.status_code}: {response.text}")
                return False
        except Exception as e:
            print(f"[jira error] Exception assigning ticket: {e}")
            return False

    def get_account_id_by_email(self, email: str) -> Optional[str]:
        """Fetch Jira accountId for a given email address using Jira API."""
        self.reload()
        if not self.is_configured:
            print(f"[stub] Would lookup Jira accountId for email: {email}")
            return f"stub_acc_{email.split('@')[0]}"

        try:
            url = f"{self.jira_url}/rest/api/3/user/search"
            response = requests.get(url, params={"query": email}, headers=self.headers, timeout=10)
            if response.status_code == 200:
                users = response.json()
                if users and isinstance(users, list):
                    account_id = users[0].get("accountId")
                    if account_id:
                        print(f"[jira] Resolved email '{email}' to account ID: {account_id}")
                        return account_id
        except Exception as e:
            print(f"[jira error] Error looking up email '{email}': {e}")
        return self.my_account_id or f"stub_acc_{email.split('@')[0]}"

    def get_user_open_ticket_count(self, account_id: str) -> int:
        """Return count of active non-completed Jira tickets for a user via JQL."""
        self.reload()
        if not self.is_configured:
            return 0

        target_id = account_id
        if ("user_acc_" in account_id or "account-id-for-" in account_id or "stub_acc_" in account_id) and self.my_account_id:
            target_id = self.my_account_id

        try:
            url = f"{self.jira_url}/rest/api/3/search/jql"
            payload = {
                "jql": f'assignee = "{target_id}" AND statusCategory != Done'
            }
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                total = data.get("total")
                if total is not None:
                    return int(total)
                issues = data.get("issues") or data.get("values") or []
                return len(issues)
        except Exception as e:
            print(f"[jira error] Error querying open ticket count for '{account_id}': {e}")
        return 0

