"""
Integration tests for External Agent Connectivity.

Tests the full lifecycle:
  1. A2A discovery (/.well-known/agent.json)
  2. External agent registration
  3. Task submission with webhook forwarding
  4. Task result callback
  5. Webhook dispatcher status
  6. HMAC signature verification

Run with:
  cd /path/to/bhrm--H
  python -m pytest tests/test_external_agent.py -v
"""

import json
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock

# Ensure backend src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "src"))

from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """Create a TestClient for the FastAPI app."""
    # Ensure we're in dev mode (no API key required)
    os.environ.pop("CORTEX_API_KEY", None)
    from api import app
    with TestClient(app) as c:
        yield c


# ═════════════════════════════════════════════════════════════════
# 1. A2A Discovery
# ═════════════════════════════════════════════════════════════════

class TestA2ADiscovery:
    def test_well_known_endpoint_returns_200(self, client):
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200

    def test_discovery_contains_mcp_info(self, client):
        resp = client.get("/.well-known/agent.json")
        data = resp.json()
        assert "mcp" in data
        assert "sse_endpoint" in data["mcp"]
        assert "/mcp/sse" in data["mcp"]["sse_endpoint"]

    def test_discovery_contains_endpoints(self, client):
        resp = client.get("/.well-known/agent.json")
        data = resp.json()
        endpoints = data["endpoints"]
        assert "register" in endpoints
        assert "submit_task" in endpoints
        assert "task_result" in endpoints
        assert "mcp_sse" in endpoints
        assert "feedback" in endpoints

    def test_discovery_contains_security_schemes(self, client):
        resp = client.get("/.well-known/agent.json")
        data = resp.json()
        schemes = data["securitySchemes"]
        assert "apiKey" in schemes
        assert "webhookHmac" in schemes

    def test_discovery_version_is_updated(self, client):
        resp = client.get("/.well-known/agent.json")
        data = resp.json()
        assert data["version"] == "2.11.0"


# ═════════════════════════════════════════════════════════════════
# 2. External Agent Registration
# ═════════════════════════════════════════════════════════════════

class TestExternalAgentRegistration:
    def test_register_external_agent(self, client):
        resp = client.post("/api/agents/external/register", json={
            "agent_id": "test-ext-agent",
            "display_name": "Test External Agent",
            "description": "A test agent for integration tests",
            "url": "http://localhost:9999/webhook",
            "auth_token": "test-bearer-token-123",
            "role": "specialist",
            "department": "shared",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "registered"
        assert data["agent_id"] == "test-ext-agent"

    def test_registered_agent_appears_in_list(self, client):
        resp = client.get("/api/agents")
        data = resp.json()
        agent_ids = [a["agent_id"] for a in data.get("agents", data.get("data", []))]
        assert "test-ext-agent" in agent_ids

    def test_registered_agent_has_agent_card(self, client):
        resp = client.get("/api/agents/test-ext-agent/agent-card")
        assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════
# 3. Task Submission
# ═════════════════════════════════════════════════════════════════

class TestTaskSubmission:
    def test_submit_task_to_external_agent(self, client):
        """Submit a task to the external agent. Since the webhook URL
        is not reachable in tests, expect dispatched status."""
        resp = client.post("/api/agents/test-ext-agent/tasks", json={
            "description": "Summarize Q4 sales data",
            "task_type": "query",
            "payload": {"quarter": "Q4"},
        })
        assert resp.status_code == 200
        data = resp.json()
        # Should be dispatched (via retry queue) or forward_failed
        assert data["status"] in ("dispatched", "forward_failed", "forwarded")
        assert "task_id" in data

    def test_submit_task_to_unknown_agent_returns_404(self, client):
        resp = client.post("/api/agents/nonexistent-agent/tasks", json={
            "description": "This should fail",
        })
        assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════
# 4. Task Result Callback
# ═════════════════════════════════════════════════════════════════

class TestTaskResultCallback:
    def test_submit_and_receive_result(self, client):
        # First, submit a task to get a task_id
        submit_resp = client.post("/api/agents/test-ext-agent/tasks", json={
            "description": "Test task for result callback",
            "task_type": "query",
        })
        task_id = submit_resp.json().get("task_id")
        assert task_id is not None

        # Now POST a result back
        result_resp = client.post(f"/api/tasks/{task_id}/result", json={
            "agent_id": "test-ext-agent",
            "status": "completed",
            "result": "Task completed successfully with results: ..."
        })
        assert result_resp.status_code == 200
        assert result_resp.json()["status"] == "accepted"

    def test_get_task_result(self, client):
        # Submit + complete a task
        submit_resp = client.post("/api/agents/test-ext-agent/tasks", json={
            "description": "Result retrieval test",
        })
        task_id = submit_resp.json().get("task_id")

        client.post(f"/api/tasks/{task_id}/result", json={
            "agent_id": "test-ext-agent",
            "status": "completed",
            "result": "Here are the results",
        })

        # Retrieve the result
        get_resp = client.get(f"/api/tasks/{task_id}/result")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["status"] == "completed"
        assert "Here are the results" in data["result_text"]

    def test_failed_task_result(self, client):
        submit_resp = client.post("/api/agents/test-ext-agent/tasks", json={
            "description": "This task will fail",
        })
        task_id = submit_resp.json().get("task_id")

        result_resp = client.post(f"/api/tasks/{task_id}/result", json={
            "agent_id": "test-ext-agent",
            "status": "failed",
            "error": "Database connection timeout",
        })
        assert result_resp.status_code == 200

    def test_nonexistent_task_result_returns_404(self, client):
        resp = client.get("/api/tasks/fake-task-id-000/result")
        assert resp.status_code == 404


# ═════════════════════════════════════════════════════════════════
# 5. Webhook Dispatcher Status
# ═════════════════════════════════════════════════════════════════

class TestDispatcherStatus:
    def test_dispatcher_status_endpoint(self, client):
        resp = client.get("/api/webhooks/dispatcher/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "queue_size" in data
        assert "dead_letters" in data


# ═════════════════════════════════════════════════════════════════
# 6. HMAC Signature Verification
# ═════════════════════════════════════════════════════════════════

class TestWebhookSecurity:
    def test_sign_and_verify_payload(self):
        from middleware.webhook_security import sign_payload, verify_signature
        import json as _json

        payload = {"task_id": "test123", "description": "hello"}
        secret = "test-secret-key"

        sig = sign_payload(payload, secret)
        assert sig.startswith("sha256=")

        # Verify with raw body
        body = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        assert verify_signature(body, sig, secret) is True

    def test_verify_rejects_wrong_secret(self):
        from middleware.webhook_security import sign_payload, verify_signature
        import json as _json

        payload = {"test": "data"}
        sig = sign_payload(payload, "correct-secret")
        body = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()

        assert verify_signature(body, sig, "wrong-secret") is False

    def test_verify_rejects_stale_timestamp(self):
        from middleware.webhook_security import sign_payload, verify_signature
        import json as _json

        payload = {"test": "data"}
        secret = "test-secret"
        sig = sign_payload(payload, secret)
        body = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()

        # Timestamp from 10 minutes ago (exceeds 5-minute window)
        old_ts = time.time() - 600
        assert verify_signature(body, sig, secret, timestamp=old_ts) is False

    def test_build_authenticated_headers(self):
        from middleware.webhook_security import build_authenticated_headers

        headers = build_authenticated_headers(
            agent_auth_token="my-token",
            webhook_secret="my-secret",
            payload={"key": "value"},
        )

        assert headers["Authorization"] == "Bearer my-token"
        assert "X-Cortex-Signature" in headers
        assert "X-Cortex-Timestamp" in headers
        assert headers["X-Cortex-Source"] == "bhrm-intelligence"

    def test_build_headers_without_secret(self):
        from middleware.webhook_security import build_authenticated_headers

        # Unset the env var to test dev mode
        os.environ.pop("CORTEX_WEBHOOK_SECRET", None)

        headers = build_authenticated_headers(payload={"key": "value"})

        assert "X-Cortex-Signature" not in headers
        assert "Authorization" not in headers
        assert headers["X-Cortex-Source"] == "bhrm-intelligence"
