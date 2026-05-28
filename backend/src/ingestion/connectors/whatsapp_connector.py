"""
WhatsApp Business Connector — Cortex Phase 1

Auth:   Meta System User permanent access token (from Meta Business Manager)
        NOT a standard OAuth flow — uses a long-lived system user token.

⚠️  CRITICAL CONSTRAINT:
    The Meta Cloud API does NOT expose message history.
    There is NO way to backfill historical conversations.
    This connector is WEBHOOK-ONLY — it ingests messages as they arrive.
    Budget should account for per-message pricing (Meta Cloud API, July 2025).

Setup required:
  1. Meta Business Account + verified phone number
  2. WhatsApp Business Account (WABA)
  3. System User token from Meta Business Manager
  4. Public webhook URL (Cortex server must be publicly reachable)

Webhook endpoint: POST /webhooks/whatsapp
Verification: X-Hub-Signature-256 HMAC-SHA256
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument

GRAPH_API = "https://graph.facebook.com/v19.0"


class WhatsAppConnector(BaseConnector):
    app_id = "whatsapp"
    display_name = "WhatsApp Business"
    auth_type = "api_key"  # Long-lived system user token — not standard OAuth

    def __init__(self, system_token: Optional[str] = None,
                 phone_number_id: Optional[str] = None,
                 app_secret: Optional[str] = None):
        super().__init__()
        self._token = system_token
        self._phone_number_id = phone_number_id
        self._app_secret = app_secret  # For webhook signature verification

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    # ── BaseConnector interface ────────────────────────────────────────────────

    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        token = credentials.get("system_token", "")
        phone_id = credentials.get("phone_number_id", "")
        app_secret = credentials.get("app_secret", "")

        if not token:
            return ConnectResult(success=False, message="No system user token provided.")
        if not phone_id:
            return ConnectResult(success=False,
                                 message="Phone number ID required. Find it in Meta Business Manager.")

        self._token = token
        self._phone_number_id = phone_id
        self._app_secret = app_secret

        try:
            import httpx
            # Verify the token by fetching phone number info
            resp = httpx.get(
                f"{GRAPH_API}/{phone_id}",
                params={"fields": "display_phone_number,verified_name"},
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                info = resp.json()
                phone = info.get("display_phone_number", phone_id)
                name = info.get("verified_name", "")
                return ConnectResult(
                    success=True,
                    message=f"Connected to WhatsApp Business: {name} ({phone}). "
                            "Webhook-only — historical messages cannot be retrieved.",
                    extra={"phone": phone, "name": name},
                )
            return ConnectResult(success=False,
                                 message=f"Meta API returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            return ConnectResult(success=False, message=f"WhatsApp API error: {e}")

    def test_connection(self) -> bool:
        try:
            import httpx
            resp = httpx.get(
                f"{GRAPH_API}/{self._phone_number_id}",
                params={"fields": "id"},
                headers=self._headers(),
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def list_resources(self) -> List[Resource]:
        """WhatsApp doesn't have selectable resources — webhook receives all messages."""
        return [Resource(
            id=self._phone_number_id or "webhook",
            name="Incoming Messages (webhook)",
            resource_type="channel",
            description="All messages received via webhook. Cannot select specific conversations.",
        )]

    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """
        WhatsApp has no history API. This method returns nothing.
        All ingestion happens via webhook (POST /webhooks/whatsapp).
        """
        print("  [WhatsApp] fetch_delta called — no-op. Messages arrive via webhook only.")
        return []

    def get_permalink(self, location_key: str) -> str:
        """WhatsApp messages don't have direct URLs — return a reference string."""
        return f"whatsapp://message/{location_key}"

    # ── Webhook processing (called by webhook_server.py) ───────────────────────

    def verify_webhook_signature(self, payload_bytes: bytes, signature_header: str) -> bool:
        """
        Verify Meta webhook signature: X-Hub-Signature-256: sha256=...
        Prevents spoofed webhook calls.
        """
        if not self._app_secret:
            return True  # Skip verification if app_secret not configured
        if not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(
            self._app_secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        received = signature_header[7:]
        return hmac.compare_digest(expected, received)

    def process_webhook_payload(self, payload: Dict[str, Any]) -> List[RawDocument]:
        """
        Parse a Meta Cloud API webhook payload into RawDocuments.
        Called by POST /webhooks/whatsapp in webhook_server.py.
        """
        documents = []
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    contacts = {
                        c["wa_id"]: c.get("profile", {}).get("name", c["wa_id"])
                        for c in value.get("contacts", [])
                    }
                    for msg in messages:
                        doc = self._message_to_document(msg, contacts)
                        if doc:
                            documents.append(doc)
        except Exception as e:
            print(f"  [WhatsApp] process_webhook_payload error: {e}")
        return documents

    def _message_to_document(self, msg: Dict[str, Any],
                              contacts: Dict[str, str]) -> Optional[RawDocument]:
        msg_id = msg.get("id", "")
        msg_type = msg.get("type", "")
        timestamp = int(msg.get("timestamp", 0))
        sender_id = msg.get("from", "")
        sender_name = contacts.get(sender_id, sender_id)
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else datetime.now(timezone.utc)

        # Only ingest text messages (skip media, audio, images)
        if msg_type != "text":
            return None

        text = msg.get("text", {}).get("body", "").strip()
        if not text or len(text) < 5:
            return None

        location_key = msg_id
        return RawDocument.build(
            location_key=location_key,
            permalink=self.get_permalink(location_key),
            title=f"WhatsApp message from {sender_name}",
            content=f"{sender_name}: {text}",
            source_app=self.app_id,
            modified_at=dt,
            extra={"sender": sender_name, "sender_id": sender_id, "msg_type": msg_type},
        )
