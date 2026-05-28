"""
Confluence Connector — Cortex Phase 1

Auth:  Atlassian OAuth2 (shared token service with Jira)
Scope: read:confluence-content.all
Delta: CQL lastModified >= "..." query (no native webhooks — poll every 30min)
Cursor: ISO timestamp of last successful sync

Ingests: spaces, pages, page trees, inline comments
Supports both Confluence Cloud and Data Center.
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from urllib.parse import urlencode

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument


class ConfluenceConnector(BaseConnector):
    app_id = "confluence"
    display_name = "Confluence"
    auth_type = "oauth2"

    def __init__(self, access_token: Optional[str] = None, cloud_id: Optional[str] = None,
                 base_url: Optional[str] = None):
        super().__init__()
        self._token = access_token
        self._cloud_id = cloud_id
        self._base_url = base_url  # For Data Center / self-hosted
        self._selected_space_keys: List[str] = []

    def _api_base(self) -> str:
        if self._base_url:
            return f"{self._base_url.rstrip('/')}/rest/api"
        if self._cloud_id:
            return f"https://api.atlassian.com/ex/confluence/{self._cloud_id}/rest/api"
        return ""

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
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
                                 message="Provide either cloud_id (Cloud) or base_url (Data Center).")
        try:
            import httpx
            resp = httpx.get(f"{self._api_base()}/user/current",
                             headers=self._headers(), timeout=10)
            if resp.status_code == 200:
                user = resp.json()
                display = user.get("displayName") or user.get("username", "unknown")
                return ConnectResult(success=True,
                                     message=f"Connected to Confluence as {display}",
                                     extra={"user": display})
            return ConnectResult(success=False,
                                 message=f"Confluence API returned {resp.status_code}")
        except Exception as e:
            return ConnectResult(success=False, message=f"Confluence error: {e}")

    def test_connection(self) -> bool:
        try:
            import httpx
            resp = httpx.get(f"{self._api_base()}/user/current",
                             headers=self._headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """List all Confluence spaces."""
        resources = []
        try:
            import httpx
            start = 0
            while True:
                resp = httpx.get(
                    f"{self._api_base()}/space",
                    params={"limit": 50, "start": start, "type": "global"},
                    headers=self._headers(), timeout=15
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                for space in data.get("results", []):
                    resources.append(Resource(
                        id=space["key"],
                        name=space.get("name", space["key"]),
                        resource_type="folder",
                        description=space.get("description", {}).get("plain", {}).get("value", ""),
                    ))
                size = data.get("size", 0)
                start += size
                if start >= data.get("totalSize", 0):
                    break
        except Exception as e:
            print(f"  [Confluence] list_resources error: {e}")
        return resources

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """Fetch pages modified since `since` using CQL lastModified filter."""
        space_filter = ""
        if self._selected_space_keys:
            spaces = ",".join(f'"{k}"' for k in self._selected_space_keys)
            space_filter = f'space.key in ({spaces}) AND '

        date_filter = ""
        if since:
            # CQL date format: "2024-01-01 00:00"
            since_formatted = since[:16].replace("T", " ")
            date_filter = f'lastModified >= "{since_formatted}" AND '

        cql = f"{space_filter}{date_filter}type = page ORDER BY lastModified DESC"

        documents: List[RawDocument] = []
        try:
            import httpx
            start = 0
            while True:
                resp = httpx.get(
                    f"{self._api_base()}/content/search",
                    params={
                        "cql": cql,
                        "limit": 50,
                        "start": start,
                        "expand": "body.storage,history,space,ancestors",
                    },
                    headers=self._headers(),
                    timeout=20,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                for page in data.get("results", []):
                    doc = self._page_to_document(page)
                    if doc:
                        documents.append(doc)
                size = data.get("size", 0)
                start += size
                if start >= data.get("totalSize", 0) or size == 0:
                    break
        except Exception as e:
            print(f"  [Confluence] fetch_delta error: {e}")

        if documents:
            self._last_sync_cursor = self.now_iso()

        return documents

    def get_permalink(self, location_key: str) -> str:
        base = self._base_url or f"https://your-domain.atlassian.net/wiki"
        return f"{base}/pages/{location_key}"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _page_to_document(self, page: Dict[str, Any]) -> Optional[RawDocument]:
        page_id = page.get("id", "")
        title = page.get("title", "Untitled")
        space = page.get("space", {}).get("key", "")
        history = page.get("history", {})
        last_updated = history.get("lastUpdated", {}).get("when", self.now_iso())
        dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))

        # Extract plain text from storage format (simplified — strips XML tags)
        body_html = page.get("body", {}).get("storage", {}).get("value", "")
        content = self._strip_html(body_html)
        if not content.strip():
            return None

        link = page.get("_links", {}).get("webui", "")
        base = self._base_url or "https://your-domain.atlassian.net/wiki"
        permalink = f"{base}{link}" if link else self.get_permalink(page_id)

        return RawDocument.build(
            location_key=page_id,
            permalink=permalink,
            title=f"[Confluence/{space}] {title}",
            content=content,
            source_app=self.app_id,
            modified_at=dt,
            resource_id=space,
            extra={"space": space, "page_id": page_id},
        )

    @staticmethod
    def _strip_html(html: str) -> str:
        """Rough HTML → plain text (no dependency on beautifulsoup)."""
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()

    def set_spaces(self, space_keys: List[str]) -> None:
        self._selected_space_keys = space_keys
