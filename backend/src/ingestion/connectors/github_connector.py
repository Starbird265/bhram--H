"""
GitHub Connector — Cortex Phase 1

Auth:   GitHub OAuth2 (read-only scopes)
Scopes: repo (read), read:org, read:user — NO write access
Delta:  GitHub Events API (30-event sliding window) + created/updated_at queries
Cursor: ISO timestamp of last sync

Ingests: PRs, issues, discussions, READMEs, wikis, changelogs
Maps repositories to departments based on team ownership.
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument

GITHUB_API = "https://api.github.com"


class GitHubConnector(BaseConnector):
    app_id = "github"
    display_name = "GitHub"
    auth_type = "oauth2"

    def __init__(self, access_token: Optional[str] = None):
        super().__init__()
        self._token = access_token
        self._selected_repos: List[str] = []  # "owner/repo" format

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        import httpx
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        resp = httpx.get(url, params=params, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── BaseConnector interface ────────────────────────────────────────────────

    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        token = credentials.get("access_token") or credentials.get("api_token", "")
        if not token:
            return ConnectResult(success=False, message="No access token provided.")
        self._token = token
        try:
            user = self._get("/user")
            login = user.get("login", "unknown")
            return ConnectResult(success=True,
                                 message=f"Connected to GitHub as @{login}",
                                 extra={"login": login, "name": user.get("name", "")})
        except Exception as e:
            return ConnectResult(success=False, message=f"GitHub error: {e}")

    def test_connection(self) -> bool:
        try:
            self._get("/user")
            return True
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """List repositories the authenticated user has access to."""
        resources = []
        try:
            page = 1
            while len(resources) < 200:
                repos = self._get("/user/repos", {
                    "per_page": 50, "page": page,
                    "sort": "pushed", "affiliation": "owner,collaborator,organization_member",
                })
                if not repos:
                    break
                for repo in repos:
                    resources.append(Resource(
                        id=repo["full_name"],
                        name=repo["full_name"],
                        resource_type="repo",
                        description=repo.get("description") or "",
                        is_private=repo.get("private", False),
                    ))
                page += 1
                if len(repos) < 50:
                    break
        except Exception as e:
            print(f"  [GitHub] list_resources error: {e}")
        return resources

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """Fetch issues, PRs, and READMEs updated since `since`."""
        repos = self._selected_repos
        if not repos:
            resources = self.list_resources()
            repos = [r.id for r in resources[:10]]  # Default: first 10

        documents: List[RawDocument] = []
        for repo in repos:
            documents.extend(self._fetch_repo(repo, since=since))

        if documents:
            self._last_sync_cursor = self.now_iso()

        return documents

    def get_permalink(self, location_key: str) -> str:
        """location_key format: 'owner/repo/issues/123' or 'owner/repo/README'"""
        if not location_key.startswith("http"):
            return f"https://github.com/{location_key}"
        return location_key

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_repo(self, full_name: str, since: Optional[str] = None) -> List[RawDocument]:
        documents = []
        try:
            # Fetch issues and PRs
            params: Dict[str, Any] = {
                "state": "all", "per_page": 50, "sort": "updated",
            }
            if since:
                params["since"] = since

            items = self._get(f"/repos/{full_name}/issues", params)
            for item in items:
                doc = self._issue_to_document(item, full_name)
                if doc:
                    documents.append(doc)

            # Fetch README
            try:
                readme = self._get(f"/repos/{full_name}/readme")
                import base64
                content = base64.b64decode(readme.get("content", "")).decode("utf-8", errors="replace")
                if content.strip():
                    updated = datetime.now(timezone.utc)
                    documents.append(RawDocument.build(
                        location_key=f"{full_name}/README",
                        permalink=f"https://github.com/{full_name}#readme",
                        title=f"README: {full_name}",
                        content=content[:10_000],
                        source_app=self.app_id,
                        modified_at=updated,
                        resource_id=full_name,
                    ))
            except Exception:
                pass  # No README — fine

        except Exception as e:
            print(f"  [GitHub] fetch_repo {full_name} error: {e}")
        return documents

    def _issue_to_document(self, item: Dict[str, Any], repo: str) -> Optional[RawDocument]:
        number = item.get("number", 0)
        title = item.get("title", "")
        body = (item.get("body") or "").strip()
        is_pr = "pull_request" in item
        type_label = "PR" if is_pr else "Issue"
        state = item.get("state", "open")
        updated = item.get("updated_at", self.now_iso())
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        url = item.get("html_url", f"https://github.com/{repo}/issues/{number}")
        user = item.get("user", {}).get("login", "unknown")
        labels = ", ".join(l.get("name", "") for l in item.get("labels", []))

        parts = [
            f"# [{type_label} #{number}] {title}",
            f"**State:** {state}  **Author:** @{user}",
        ]
        if labels:
            parts.append(f"**Labels:** {labels}")
        if body:
            parts.append(f"\n{body[:3000]}")

        content = "\n".join(parts)
        if not content.strip():
            return None

        return RawDocument.build(
            location_key=f"{repo}/issues/{number}",
            permalink=url,
            title=f"[{repo}] {title}",
            content=content,
            source_app=self.app_id,
            modified_at=dt,
            resource_id=repo,
            extra={"type": type_label, "state": state, "number": number},
        )

    def set_repos(self, repo_full_names: List[str]) -> None:
        """Set repos to sync in 'owner/repo' format."""
        self._selected_repos = repo_full_names
