"""
Slack OAuth Connector — Cortex Phase 1

Auth:  OAuth 2.0 Bot token (xoxb-...)
Scopes: channels:history channels:read groups:history im:history users:read files:read
        — NO admin:read, NO write scopes.

Delta sync strategy:
  PRIMARY:  Events API webhook → real-time ingestion at /webhooks/slack
  FALLBACK: conversations.history with `oldest` param (cursor-based)

Cursor: Slack `ts` Unix timestamp string (e.g. "1716200000.000000")
Permalink: https://{workspace}.slack.com/archives/{channel_id}/p{ts_nodot}
"""

from __future__ import annotations

import time
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument


class SlackConnector(BaseConnector):
    app_id = "slack"
    display_name = "Slack"
    auth_type = "oauth2"

    # Scopes we request — principle of least privilege
    REQUIRED_SCOPES = [
        "channels:history", "channels:read", "groups:history",
        "im:history", "users:read", "files:read",
    ]

    def __init__(self, bot_token: Optional[str] = None, workspace_url: Optional[str] = None):
        super().__init__()
        self._token = bot_token
        self._workspace_url = workspace_url or ""
        self._client = None
        self._selected_channel_ids: List[str] = []

    def _get_client(self):
        if self._client is None:
            try:
                from slack_sdk import WebClient
                self._client = WebClient(token=self._token)
            except ImportError:
                raise RuntimeError("slack_sdk not installed. Run: pip install slack_sdk")
        return self._client

    # ── BaseConnector interface ────────────────────────────────────────────────

    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        token = credentials.get("bot_token") or credentials.get("access_token", "")
        if not token.startswith("xoxb-"):
            return ConnectResult(success=False,
                                 message="Invalid Slack token. Must start with 'xoxb-'.")
        self._token = token
        self._client = None
        try:
            client = self._get_client()
            resp = client.auth_test()
            workspace = resp.get("team", "unknown")
            self._workspace_url = resp.get("url", "")
            return ConnectResult(success=True,
                                 message=f"Connected to Slack workspace: {workspace}",
                                 extra={"workspace": workspace, "url": self._workspace_url})
        except Exception as e:
            return ConnectResult(success=False, message=f"Slack auth failed: {e}")

    def test_connection(self) -> bool:
        try:
            self._get_client().auth_test()
            return True
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """List all public and private channels the bot has access to."""
        resources = []
        try:
            client = self._get_client()
            cursor = None
            while True:
                kwargs: Dict[str, Any] = {
                    "types": "public_channel,private_channel",
                    "limit": 200,
                    "exclude_archived": True,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                resp = client.conversations_list(**kwargs)
                for ch in resp.get("channels", []):
                    resources.append(Resource(
                        id=ch["id"],
                        name=f"#{ch['name']}",
                        resource_type="channel",
                        description=ch.get("purpose", {}).get("value", ""),
                        member_count=ch.get("num_members"),
                        is_private=ch.get("is_private", False),
                    ))
                meta = resp.get("response_metadata", {})
                cursor = meta.get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            print(f"  [Slack] list_resources error: {e}")
        return resources

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """
        Fetch messages from selected channels since `since` cursor (Slack ts).
        If no channels selected, fetches all joined public channels (up to 10).
        """
        channel_ids = self._selected_channel_ids
        if not channel_ids:
            # Fall back: use all resources, capped at 10
            all_resources = self.list_resources()
            channel_ids = [r.id for r in all_resources if not r.is_private][:10]

        documents: List[RawDocument] = []
        for channel_id in channel_ids:
            docs = self._fetch_channel(channel_id, oldest=since)
            documents.extend(docs)

        if documents:
            # Update cursor to the latest ts seen
            latest_ts = max(d.extra.get("ts", "0") for d in documents)
            self._last_sync_cursor = latest_ts

        return documents

    def get_permalink(self, location_key: str) -> str:
        """location_key format: '{channel_id}/{ts}'"""
        if "/" not in location_key:
            return ""
        channel_id, ts = location_key.split("/", 1)
        ts_nodot = ts.replace(".", "")
        base = self._workspace_url.rstrip("/") or "https://slack.com"
        return f"{base}/archives/{channel_id}/p{ts_nodot}"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_channel(self, channel_id: str, oldest: Optional[str] = None) -> List[RawDocument]:
        documents = []
        try:
            client = self._get_client()
            kwargs: Dict[str, Any] = {"channel": channel_id, "limit": 200}
            if oldest:
                kwargs["oldest"] = oldest

            resp = client.conversations_history(**kwargs)
            messages = resp.get("messages", [])

            # Also fetch thread replies for messages with reply_count > 0
            for msg in messages:
                if msg.get("reply_count", 0) > 0:
                    thread_resp = client.conversations_replies(
                        channel=channel_id, ts=msg["ts"], limit=100
                    )
                    messages.extend(thread_resp.get("messages", [])[1:])  # skip parent

            for msg in messages:
                if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                    continue
                text = msg.get("text", "").strip()
                if not text or len(text) < 10:
                    continue

                ts = msg["ts"]
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                location_key = f"{channel_id}/{ts}"

                documents.append(RawDocument.build(
                    location_key=location_key,
                    permalink=self.get_permalink(location_key),
                    title=f"Slack message in {channel_id}",
                    content=text,
                    source_app=self.app_id,
                    modified_at=dt,
                    resource_id=channel_id,
                    extra={"ts": ts, "user": msg.get("user", "")},
                ))
        except Exception as e:
            print(f"  [Slack] fetch_channel {channel_id} error: {e}")
        return documents

    def set_channels(self, channel_ids: List[str]) -> None:
        """Set which channels to sync (called after user makes selection in UI)."""
        self._selected_channel_ids = channel_ids
