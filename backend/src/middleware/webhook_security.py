"""
Webhook Security — HMAC Signing & Verification

Provides utilities for:
  - HMAC-SHA256 signing of outbound webhook payloads
  - Signature verification for inbound webhooks
  - Building authenticated headers for external agent calls

Security model:
  - CORTEX_WEBHOOK_SECRET env var → HMAC signing (global)
  - Per-agent auth_token → Bearer token in Authorization header
  - X-Cortex-Timestamp → Replay protection (5-minute window)
"""

import hashlib
import hmac
import json
import os
import time
from typing import Optional


def sign_payload(payload: dict, secret: str) -> str:
    """
    HMAC-SHA256 signature of a JSON-serialized payload.

    Args:
        payload: Dictionary to sign.
        secret: The shared secret key.

    Returns:
        Hex-encoded HMAC-SHA256 signature prefixed with 'sha256='.
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


def verify_signature(
    raw_body: bytes,
    signature: str,
    secret: str,
    max_age_seconds: int = 300,
    timestamp: Optional[float] = None,
) -> bool:
    """
    Verify an incoming webhook signature.

    Args:
        raw_body: Raw request body bytes.
        signature: The X-Cortex-Signature header value (sha256=...).
        secret: The shared secret key.
        max_age_seconds: Maximum age of the timestamp before rejection.
        timestamp: The X-Cortex-Timestamp header value (epoch float).

    Returns:
        True if valid, False otherwise.
    """
    if not signature or not signature.startswith("sha256="):
        return False

    # Replay protection
    if timestamp is not None:
        age = abs(time.time() - timestamp)
        if age > max_age_seconds:
            return False

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    provided = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def build_authenticated_headers(
    agent_auth_token: Optional[str] = None,
    webhook_secret: Optional[str] = None,
    payload: Optional[dict] = None,
) -> dict:
    """
    Build headers for outbound webhook calls to external agents.

    Headers produced:
      - Content-Type: application/json                     (always)
      - Authorization: Bearer <agent_auth_token>           (if agent provided one)
      - X-Cortex-Signature: sha256=<hmac>                  (if CORTEX_WEBHOOK_SECRET set)
      - X-Cortex-Timestamp: <epoch>                        (if signing)
      - X-Cortex-Source: bhrm-intelligence                 (always)

    Args:
        agent_auth_token: Bearer token the external agent expects.
        webhook_secret: Global HMAC secret (from env or passed explicitly).
        payload: The dict being sent (needed for signing).

    Returns:
        Dictionary of HTTP headers.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Cortex-Source": "bhrm-intelligence",
    }

    if agent_auth_token:
        headers["Authorization"] = f"Bearer {agent_auth_token}"

    secret = webhook_secret or os.getenv("CORTEX_WEBHOOK_SECRET", "")
    if secret and payload is not None:
        ts = time.time()
        headers["X-Cortex-Timestamp"] = str(ts)
        headers["X-Cortex-Signature"] = sign_payload(payload, secret)

    return headers
