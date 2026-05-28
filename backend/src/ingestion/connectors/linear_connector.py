"""
Linear Connector — Cortex Phase 1

Auth:  Linear OAuth2 (read scope only)
API:   GraphQL (https://api.linear.app/graphql)
Delta: GraphQL updatedAt_gte filter
Cursor: ISO timestamp of last successful sync

Ingests: issues, comments, project updates, cycles, team descriptions
Maps Linear teams → departments for the knowledge pipeline.
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument

LINEAR_API = "https://api.linear.app/graphql"


class LinearConnector(BaseConnector):
    app_id = "linear"
    display_name = "Linear"
    auth_type = "oauth2"

    def __init__(self, access_token: Optional[str] = None):
        super().__init__()
        self._token = access_token
        self._selected_team_ids: List[str] = []

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _query(self, gql: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        import httpx
        resp = httpx.post(
            LINEAR_API,
            json={"query": gql, "variables": variables or {}},
            headers=self._headers(),
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data.get("data", {})

    # ── BaseConnector interface ────────────────────────────────────────────────

    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        token = credentials.get("access_token") or credentials.get("api_key", "")
        if not token:
            return ConnectResult(success=False, message="No access token provided.")
        self._token = token
        try:
            data = self._query("{ viewer { id name email } }")
            viewer = data.get("viewer", {})
            name = viewer.get("name") or viewer.get("email", "unknown")
            return ConnectResult(success=True,
                                 message=f"Connected to Linear as {name}",
                                 extra={"user": name})
        except Exception as e:
            return ConnectResult(success=False, message=f"Linear auth error: {e}")

    def test_connection(self) -> bool:
        try:
            self._query("{ viewer { id } }")
            return True
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """List all Linear teams."""
        resources = []
        try:
            data = self._query("""
            { teams { nodes { id name description members { totalCount } } } }
            """)
            for team in data.get("teams", {}).get("nodes", []):
                resources.append(Resource(
                    id=team["id"],
                    name=team["name"],
                    resource_type="project",
                    description=team.get("description") or "",
                    member_count=team.get("members", {}).get("totalCount"),
                ))
        except Exception as e:
            print(f"  [Linear] list_resources error: {e}")
        return resources

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """Fetch issues and comments updated since `since`."""
        documents: List[RawDocument] = []
        team_filter = ""
        if self._selected_team_ids:
            team_ids_str = json_list(self._selected_team_ids)
            team_filter = f'team: {{ id: {{ in: [{team_ids_str}] }} }},'

        updated_filter = ""
        if since:
            updated_filter = f'updatedAt: {{ gte: "{since}" }},'

        query = f"""
        {{
          issues(filter: {{ {team_filter} {updated_filter} }} first: 100) {{
            nodes {{
              id
              title
              description
              state {{ name }}
              priority
              updatedAt
              url
              team {{ id name }}
              comments {{
                nodes {{
                  id body createdAt user {{ name }} url
                }}
              }}
            }}
          }}
        }}
        """
        try:
            data = self._query(query)
            for issue in data.get("issues", {}).get("nodes", []):
                doc = self._issue_to_document(issue)
                if doc:
                    documents.append(doc)
        except Exception as e:
            print(f"  [Linear] fetch_delta error: {e}")

        if documents:
            self._last_sync_cursor = self.now_iso()

        return documents

    def get_permalink(self, location_key: str) -> str:
        """location_key is the Linear issue URL."""
        return location_key if location_key.startswith("http") else f"https://linear.app/issue/{location_key}"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _issue_to_document(self, issue: Dict[str, Any]) -> Optional[RawDocument]:
        issue_id = issue["id"]
        title = issue.get("title", "Untitled Issue")
        description = issue.get("description") or ""
        state = issue.get("state", {}).get("name", "")
        team_name = issue.get("team", {}).get("name", "")
        url = issue.get("url", f"https://linear.app/issue/{issue_id}")
        updated = issue.get("updatedAt", self.now_iso())
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))

        # Combine description + comments into one rich document
        parts = [f"# {title}", f"**Status:** {state}  **Team:** {team_name}", ""]
        if description:
            parts.append(description)

        comments = issue.get("comments", {}).get("nodes", [])
        if comments:
            parts.append("\n## Comments")
            for c in comments:
                author = c.get("user", {}).get("name", "Someone")
                body = c.get("body", "").strip()
                if body:
                    parts.append(f"**{author}:** {body}")

        content = "\n".join(parts)
        if not content.strip():
            return None

        return RawDocument.build(
            location_key=url,
            permalink=url,
            title=title,
            content=content,
            source_app=self.app_id,
            modified_at=dt,
            resource_id=issue.get("team", {}).get("id", ""),
            extra={"state": state, "team": team_name},
        )

    def set_teams(self, team_ids: List[str]) -> None:
        self._selected_team_ids = team_ids


def json_list(items: List[str]) -> str:
    return ", ".join(f'"{i}"' for i in items)
