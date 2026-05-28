"""
MS Teams Connector — Cortex Phase 1

Auth:  Azure AD OAuth2 (Microsoft identity platform)
Scopes: Channel.ReadBasic.All ChannelMessage.Read.All Files.Read.All offline_access
API:   Microsoft Graph API
Delta: Graph API delta tokens ($deltaToken)

Ingests: team channels, channel messages, SharePoint files linked in Teams
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument

GRAPH_API = "https://graph.microsoft.com/v1.0"


class MSTeamsConnector(BaseConnector):
    app_id = "ms_teams"
    display_name = "Microsoft Teams"
    auth_type = "oauth2"

    def __init__(self, access_token: Optional[str] = None):
        super().__init__()
        self._token = access_token
        self._selected_channel_ids: List[Dict[str, str]] = []  # [{team_id, channel_id}]
        self._delta_token: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _get(self, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        import httpx
        resp = httpx.get(url, params=params, headers=self._headers(), timeout=20)
        resp.raise_for_status()
        return resp.json()

    # ── BaseConnector interface ────────────────────────────────────────────────

    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        token = credentials.get("access_token", "")
        if not token:
            return ConnectResult(success=False, message="No access token provided.")
        self._token = token
        try:
            data = self._get(f"{GRAPH_API}/me")
            name = data.get("displayName") or data.get("userPrincipalName", "unknown")
            return ConnectResult(success=True,
                                 message=f"Connected to MS Teams as {name}",
                                 extra={"user": name})
        except Exception as e:
            return ConnectResult(success=False, message=f"MS Teams error: {e}")

    def test_connection(self) -> bool:
        try:
            self._get(f"{GRAPH_API}/me")
            return True
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """List all Teams and their channels."""
        resources = []
        try:
            teams = self._get(f"{GRAPH_API}/me/joinedTeams").get("value", [])
            for team in teams:
                team_id = team["id"]
                team_name = team.get("displayName", "Unknown Team")
                channels = self._get(
                    f"{GRAPH_API}/teams/{team_id}/channels"
                ).get("value", [])
                for ch in channels:
                    resources.append(Resource(
                        id=f"{team_id}/{ch['id']}",
                        name=f"{team_name} › {ch.get('displayName', 'General')}",
                        resource_type="channel",
                        description=ch.get("description", ""),
                        parent_id=team_id,
                    ))
        except Exception as e:
            print(f"  [MSTeams] list_resources error: {e}")
        return resources

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """Fetch messages from selected channels using Graph delta tokens."""
        documents: List[RawDocument] = []
        channel_pairs = self._selected_channel_ids

        if not channel_pairs:
            # No selection: fetch from first 5 channels
            resources = self.list_resources()
            channel_pairs = [
                {"team_id": r.id.split("/")[0], "channel_id": r.id.split("/")[1]}
                for r in resources[:5]
            ]

        for pair in channel_pairs:
            docs = self._fetch_channel_messages(
                pair["team_id"], pair["channel_id"], since=since
            )
            documents.extend(docs)

        if documents:
            self._last_sync_cursor = self.now_iso()

        return documents

    def get_permalink(self, location_key: str) -> str:
        """location_key format: '{team_id}/{channel_id}/{message_id}'"""
        parts = location_key.split("/")
        if len(parts) >= 3:
            team_id, channel_id, msg_id = parts[0], parts[1], parts[2]
            return (f"https://teams.microsoft.com/l/message/"
                    f"{channel_id}/{msg_id}?groupId={team_id}")
        return f"https://teams.microsoft.com"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_channel_messages(self, team_id: str, channel_id: str,
                                since: Optional[str] = None) -> List[RawDocument]:
        documents = []
        try:
            url = f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages/delta"
            params: Dict[str, str] = {}
            if self._delta_token:
                params["$deltaToken"] = self._delta_token
            elif since:
                params["$filter"] = f"lastModifiedDateTime ge {since}"

            while url:
                data = self._get(url, params=params)
                params = {}  # Only used on first call
                for msg in data.get("value", []):
                    doc = self._message_to_document(msg, team_id, channel_id)
                    if doc:
                        documents.append(doc)
                # Handle pagination
                url = data.get("@odata.nextLink")
                # Save delta token for next run
                delta_link = data.get("@odata.deltaLink", "")
                if delta_link and "$deltaToken=" in delta_link:
                    self._delta_token = delta_link.split("$deltaToken=")[-1]
        except Exception as e:
            print(f"  [MSTeams] fetch_channel {channel_id} error: {e}")
        return documents

    def _message_to_document(self, msg: Dict[str, Any],
                             team_id: str, channel_id: str) -> Optional[RawDocument]:
        msg_id = msg.get("id", "")
        body = msg.get("body", {})
        content_type = body.get("contentType", "text")
        text = body.get("content", "").strip()

        # Strip HTML tags if content is HTML
        if content_type == "html":
            import re
            text = re.sub(r"<[^>]+>", " ", text).strip()

        if not text or len(text) < 5:
            return None

        sender = msg.get("from", {})
        sender_name = (sender.get("user", {}).get("displayName") or
                       sender.get("application", {}).get("displayName") or "Someone")
        created = msg.get("createdDateTime", self.now_iso())
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))

        location_key = f"{team_id}/{channel_id}/{msg_id}"
        return RawDocument.build(
            location_key=location_key,
            permalink=self.get_permalink(location_key),
            title=f"Teams message from {sender_name}",
            content=f"{sender_name}: {text}",
            source_app=self.app_id,
            modified_at=dt,
            resource_id=f"{team_id}/{channel_id}",
            extra={"sender": sender_name},
        )

    def set_channels(self, channel_pairs: List[Dict[str, str]]) -> None:
        """Set channels to sync. Each item: {team_id, channel_id}."""
        self._selected_channel_ids = channel_pairs
