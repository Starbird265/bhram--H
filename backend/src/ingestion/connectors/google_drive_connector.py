"""
Google Drive Connector — Cortex Phase 1

Auth:  OAuth2 PKCE (drive.readonly scope only — NO write)
Delta: Drive push notification channels → fallback: modifiedTime query
Cursor: ISO timestamp of last successful sync (modifiedTime >= filter)

Ingests:
  - Google Docs  → markdown (via export API)
  - Google Sheets → CSV summary
  - PDFs         → text extract
  - Plain text / Markdown files → raw content
"""

from __future__ import annotations

import io
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument


DRIVE_API = "https://www.googleapis.com/drive/v3"
DOCS_EXPORT_URL = "https://docs.google.com/document/d/{id}/export?format=md"
SHEETS_EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv"

SUPPORTED_MIME_TYPES = {
    "application/vnd.google-apps.document": "gdoc",
    "application/vnd.google-apps.spreadsheet": "gsheet",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-markdown": "md",
}


class GoogleDriveConnector(BaseConnector):
    app_id = "google_drive"
    display_name = "Google Drive"
    auth_type = "oauth2"

    def __init__(self, access_token: Optional[str] = None):
        super().__init__()
        self._token = access_token
        self._selected_folder_ids: List[str] = []

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    # ── BaseConnector interface ────────────────────────────────────────────────

    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        token = credentials.get("access_token", "")
        if not token:
            return ConnectResult(success=False, message="No access token provided.")
        self._token = token
        try:
            import httpx
            resp = httpx.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers=self._headers(), timeout=10
            )
            if resp.status_code == 200:
                info = resp.json()
                email = info.get("email", "unknown")
                return ConnectResult(success=True,
                                     message=f"Connected to Google Drive as {email}",
                                     extra={"email": email})
            return ConnectResult(success=False,
                                 message=f"Google API returned {resp.status_code}")
        except Exception as e:
            return ConnectResult(success=False, message=f"Google Drive error: {e}")

    def test_connection(self) -> bool:
        try:
            import httpx
            resp = httpx.get(f"{DRIVE_API}/about?fields=user",
                             headers=self._headers(), timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """List all folders in the user's Drive root."""
        resources = []
        try:
            import httpx
            params = {
                "q": "mimeType='application/vnd.google-apps.folder' and trashed=false",
                "fields": "files(id,name,description,owners)",
                "pageSize": 100,
            }
            resp = httpx.get(f"{DRIVE_API}/files", params=params,
                             headers=self._headers(), timeout=15)
            if resp.status_code == 200:
                for f in resp.json().get("files", []):
                    resources.append(Resource(
                        id=f["id"],
                        name=f["name"],
                        resource_type="folder",
                        description=f.get("description", ""),
                    ))
        except Exception as e:
            print(f"  [GoogleDrive] list_resources error: {e}")
        return resources

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """Fetch files modified since `since` (ISO timestamp) from selected folders."""
        documents: List[RawDocument] = []
        folder_ids = self._selected_folder_ids

        # Build file query
        conditions = ["trashed=false"]
        if folder_ids:
            parent_cond = " or ".join(f"'{fid}' in parents" for fid in folder_ids)
            conditions.append(f"({parent_cond})")
        # Filter by supported MIME types
        mime_cond = " or ".join(f"mimeType='{m}'" for m in SUPPORTED_MIME_TYPES)
        conditions.append(f"({mime_cond})")
        if since:
            conditions.append(f"modifiedTime >= '{since}'")

        query = " and ".join(conditions)

        try:
            import httpx
            page_token = None
            while True:
                params: Dict[str, Any] = {
                    "q": query,
                    "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,size)",
                    "pageSize": 100,
                    "orderBy": "modifiedTime desc",
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = httpx.get(f"{DRIVE_API}/files", params=params,
                                 headers=self._headers(), timeout=15)
                if resp.status_code != 200:
                    break
                data = resp.json()
                for f in data.get("files", []):
                    doc = self._fetch_file(f, httpx)
                    if doc:
                        documents.append(doc)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            print(f"  [GoogleDrive] fetch_delta error: {e}")

        if documents:
            self._last_sync_cursor = self.now_iso()

        return documents

    def get_permalink(self, location_key: str) -> str:
        """location_key is the Drive file ID."""
        return f"https://drive.google.com/file/d/{location_key}/view"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_file(self, file_meta: Dict[str, Any], httpx_module: Any) -> Optional[RawDocument]:
        file_id = file_meta["id"]
        mime = file_meta.get("mimeType", "")
        name = file_meta.get("name", "Untitled")
        web_link = file_meta.get("webViewLink", self.get_permalink(file_id))
        modified = file_meta.get("modifiedTime", self.now_iso())
        dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))

        content = ""
        try:
            if mime == "application/vnd.google-apps.document":
                # Export Google Doc as plain text (markdown format)
                resp = httpx_module.get(
                    f"{DRIVE_API}/files/{file_id}/export",
                    params={"mimeType": "text/plain"},
                    headers=self._headers(), timeout=30
                )
                content = resp.text if resp.status_code == 200 else ""

            elif mime == "application/vnd.google-apps.spreadsheet":
                # Export as CSV and create a summary
                resp = httpx_module.get(
                    f"{DRIVE_API}/files/{file_id}/export",
                    params={"mimeType": "text/csv"},
                    headers=self._headers(), timeout=30
                )
                if resp.status_code == 200:
                    lines = resp.text.split("\n")[:50]  # First 50 rows only
                    content = f"[Spreadsheet: {name}]\n" + "\n".join(lines)

            elif mime in ("text/plain", "text/markdown", "text/x-markdown"):
                resp = httpx_module.get(
                    f"{DRIVE_API}/files/{file_id}",
                    params={"alt": "media"},
                    headers=self._headers(), timeout=30
                )
                content = resp.text if resp.status_code == 200 else ""

            elif mime == "application/pdf":
                # Use OCR export for PDFs (Drive's built-in)
                resp = httpx_module.get(
                    f"{DRIVE_API}/files/{file_id}/export",
                    params={"mimeType": "text/plain"},
                    headers=self._headers(), timeout=30
                )
                content = resp.text if resp.status_code == 200 else ""

        except Exception as e:
            print(f"  [GoogleDrive] Failed to fetch file {file_id} ({name}): {e}")
            return None

        if not content.strip():
            return None

        # Truncate very large files to 50k chars
        if len(content) > 50_000:
            content = content[:50_000] + "\n[... content truncated ...]"

        return RawDocument.build(
            location_key=file_id,
            permalink=web_link,
            title=name,
            content=content,
            source_app=self.app_id,
            modified_at=dt,
            resource_id=file_id,
            extra={"mime_type": mime},
        )

    def set_folders(self, folder_ids: List[str]) -> None:
        """Set which folders to sync (empty = sync entire Drive)."""
        self._selected_folder_ids = folder_ids
