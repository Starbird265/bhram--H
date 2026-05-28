"""
Jira Connector — Cortex Phase 1

Auth:  Atlassian OAuth2 (shared with Confluence)
Scope: read:jira-work read:jira-user
Delta: Jira webhooks (issue:created/updated) → fallback: JQL updated >= startOfDay(-1d)
Cursor: ISO timestamp of last successful sync

Ingests: issues, comments, sprint retrospectives, epics, changelogs
Maps Jira projects → departments in the knowledge pipeline.
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument

JIRA_CLOUD_API = "https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"


class JiraConnector(BaseConnector):
    app_id = "jira"
    display_name = "Jira"
    auth_type = "oauth2"

    def __init__(self, access_token: Optional[str] = None, cloud_id: Optional[str] = None,
                 base_url: Optional[str] = None):
        super().__init__()
        self._token = access_token
        self._cloud_id = cloud_id
        self._base_url = base_url
        self._selected_project_keys: List[str] = []

    def _api_base(self) -> str:
        if self._base_url:
            return f"{self._base_url.rstrip('/')}/rest/api/3"
        if self._cloud_id:
            return JIRA_CLOUD_API.format(cloud_id=self._cloud_id)
        return ""

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ── BaseConnector interface ────────────────────────────────────────────────

    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        token = credentials.get("access_token") or credentials.get("api_token", "")
        cloud_id = credentials.get("cloud_id", "")
        base_url = credentials.get("base_url", "")
        if not token:
            return ConnectResult(success=False, message="No access token provided.")
        self._token = token
        self._cloud_id = cloud_id
        self._base_url = base_url
        if not self._api_base():
            return ConnectResult(success=False,
                                 message="Provide either cloud_id (Jira Cloud) or base_url (Jira Server).")
        try:
            import httpx
            resp = httpx.get(f"{self._api_base()}/myself",
                             headers=self._headers(), timeout=10)
            if resp.status_code == 200:
                user = resp.json()
                name = user.get("displayName") or user.get("emailAddress", "unknown")
                return ConnectResult(success=True,
                                     message=f"Connected to Jira as {name}",
                                     extra={"user": name})
            return ConnectResult(success=False,
                                 message=f"Jira API returned {resp.status_code}")
        except Exception as e:
            return ConnectResult(success=False, message=f"Jira error: {e}")

    def test_connection(self) -> bool:
        try:
            import httpx
            resp = httpx.get(f"{self._api_base()}/myself",
                             headers=self._headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """List all Jira projects."""
        resources = []
        try:
            import httpx
            resp = httpx.get(
                f"{self._api_base()}/project/search",
                params={"maxResults": 100},
                headers=self._headers(), timeout=15
            )
            if resp.status_code == 200:
                for project in resp.json().get("values", []):
                    resources.append(Resource(
                        id=project["key"],
                        name=f"{project.get('name', project['key'])} ({project['key']})",
                        resource_type="project",
                        description=project.get("description", ""),
                    ))
        except Exception as e:
            print(f"  [Jira] list_resources error: {e}")
        return resources

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """Fetch issues updated since `since` using JQL."""
        project_filter = ""
        if self._selected_project_keys:
            keys = ", ".join(f'"{k}"' for k in self._selected_project_keys)
            project_filter = f"project in ({keys}) AND "

        if since:
            since_jql = since[:10]  # YYYY-MM-DD
            date_filter = f'updated >= "{since_jql}" '
        else:
            date_filter = "updated >= startOfDay(-7d) "  # Default: last 7 days

        jql = f"{project_filter}{date_filter}ORDER BY updated DESC"

        documents: List[RawDocument] = []
        try:
            import httpx
            start_at = 0
            max_results = 50
            while True:
                resp = httpx.post(
                    f"{self._api_base()}/issue/search",
                    json={
                        "jql": jql,
                        "startAt": start_at,
                        "maxResults": max_results,
                        "fields": ["summary", "description", "status", "priority",
                                   "assignee", "reporter", "project", "updated",
                                   "comment", "issuetype", "parent"],
                    },
                    headers=self._headers(),
                    timeout=20,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                for issue in data.get("issues", []):
                    doc = self._issue_to_document(issue)
                    if doc:
                        documents.append(doc)
                total = data.get("total", 0)
                start_at += max_results
                if start_at >= total:
                    break
        except Exception as e:
            print(f"  [Jira] fetch_delta error: {e}")

        if documents:
            self._last_sync_cursor = self.now_iso()

        return documents

    def get_permalink(self, location_key: str) -> str:
        base = self._base_url or "https://your-domain.atlassian.net"
        return f"{base}/browse/{location_key}"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _issue_to_document(self, issue: Dict[str, Any]) -> Optional[RawDocument]:
        key = issue.get("key", "")
        fields = issue.get("fields", {})
        title = fields.get("summary", "Untitled")
        status = fields.get("status", {}).get("name", "")
        issue_type = fields.get("issuetype", {}).get("name", "")
        project = fields.get("project", {}).get("key", "")
        updated = fields.get("updated", self.now_iso())
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))

        # Build rich content
        parts = [
            f"# [{key}] {title}",
            f"**Type:** {issue_type}  **Status:** {status}  **Project:** {project}",
            "",
        ]

        # Description (Atlassian Document Format → plain text)
        desc = fields.get("description")
        if desc:
            desc_text = self._extract_adf_text(desc)
            if desc_text:
                parts.append(desc_text)

        # Comments
        comments = fields.get("comment", {}).get("comments", [])
        if comments:
            parts.append("\n## Comments")
            for c in comments[-10:]:  # Last 10 comments
                author = c.get("author", {}).get("displayName", "Someone")
                body = c.get("body")
                if body:
                    text = self._extract_adf_text(body)
                    if text:
                        parts.append(f"**{author}:** {text}")

        content = "\n".join(parts)
        if not content.strip():
            return None

        return RawDocument.build(
            location_key=key,
            permalink=self.get_permalink(key),
            title=f"[{project}] {title}",
            content=content,
            source_app=self.app_id,
            modified_at=dt,
            resource_id=project,
            extra={"status": status, "type": issue_type, "project": project},
        )

    @staticmethod
    def _extract_adf_text(adf: Any) -> str:
        """
        Extract plain text from Atlassian Document Format (ADF) JSON.
        ADF is a recursive structure: { type, content: [...] }
        """
        if isinstance(adf, str):
            return adf
        if isinstance(adf, dict):
            if adf.get("type") == "text":
                return adf.get("text", "")
            content = adf.get("content", [])
            if isinstance(content, list):
                return " ".join(JiraConnector._extract_adf_text(c) for c in content)
        if isinstance(adf, list):
            return " ".join(JiraConnector._extract_adf_text(c) for c in adf)
        return ""

    def set_projects(self, project_keys: List[str]) -> None:
        self._selected_project_keys = project_keys
