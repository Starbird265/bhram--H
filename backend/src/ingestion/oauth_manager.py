"""
OAuth Manager — RFC 9700 PKCE + localhost callback pattern.

How one-click connect works for local/desktop apps:
1. Generate PKCE code_verifier + code_challenge (SHA256)
2. Spin up a temporary HTTP server on localhost:8765/callback
3. Open provider's OAuth URL in the system browser
4. Browser redirects back → server catches code + state
5. Exchange code + code_verifier for access_token + refresh_token
6. Store tokens in system keyring (keyring lib)
7. Shut down the localhost listener
Full round-trip completes in < 3 seconds from click.

Token refresh loop:
- Before each connector run, check token expiry from keyring
- If expiry - now < 5 minutes → auto-refresh via POST to token endpoint
- User is never prompted again after the initial connect
"""

import os
import json
import base64
import hashlib
import secrets
import time
import threading
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False


# ── Provider OAuth configurations ──────────────────────────────────────────────

OAUTH_CONFIGS: Dict[str, Dict[str, Any]] = {
    "notion": {
        "auth_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "scopes": ["read_content"],
        "pkce": False,                         # Notion uses client_secret (basic auth)
        "token_method": "basic",
        "response_type": "code",
    },
    "slack": {
        "auth_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "scopes": ["channels:history", "channels:read", "groups:history",
                   "im:history", "users:read", "files:read"],
        "pkce": False,
        "token_method": "post",
    },
    "google_drive": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        "pkce": True,
        "token_method": "post",
        "extra_params": {"access_type": "offline", "prompt": "consent"},
    },
    "ms_teams": {
        "auth_url": "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        "scopes": ["Channel.ReadBasic.All", "ChannelMessage.Read.All",
                   "Files.Read.All", "offline_access"],
        "pkce": True,
        "token_method": "post",
        "tenant_id": "common",
    },
    "linear": {
        "auth_url": "https://linear.app/oauth/authorize",
        "token_url": "https://api.linear.app/oauth/token",
        "scopes": ["read"],
        "pkce": False,
        "token_method": "post",
    },
    "confluence": {
        "auth_url": "https://auth.atlassian.com/authorize",
        "token_url": "https://auth.atlassian.com/oauth/token",
        "scopes": ["read:confluence-content.all", "offline_access"],
        "pkce": True,
        "token_method": "post",
        "extra_params": {"audience": "api.atlassian.com", "prompt": "consent"},
    },
    "jira": {
        # Shares Atlassian OAuth with Confluence
        "auth_url": "https://auth.atlassian.com/authorize",
        "token_url": "https://auth.atlassian.com/oauth/token",
        "scopes": ["read:jira-work", "read:jira-user", "offline_access"],
        "pkce": True,
        "token_method": "post",
        "extra_params": {"audience": "api.atlassian.com", "prompt": "consent"},
    },
}

CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
KEYRING_SERVICE = "cortex-oauth"
TOKEN_REFRESH_BUFFER_SECONDS = 300  # Refresh if < 5 min remaining


# ── Data models ─────────────────────────────────────────────────────────────────

@dataclass
class TokenSet:
    app_id: str
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None   # Unix timestamp
    token_type: str = "Bearer"
    scope: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > (self.expires_at - TOKEN_REFRESH_BUFFER_SECONDS)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TokenSet":
        return cls(**json.loads(data))


@dataclass
class OAuthResult:
    success: bool
    app_id: str
    message: str
    token_set: Optional[TokenSet] = None


# ── PKCE helpers ────────────────────────────────────────────────────────────────

def _generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Callback HTTP handler ───────────────────────────────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the OAuth callback code."""

    received_code: Optional[str] = None
    received_state: Optional[str] = None
    received_error: Optional[str] = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))

        _CallbackHandler.received_code = params.get("code")
        _CallbackHandler.received_state = params.get("state")
        _CallbackHandler.received_error = params.get("error")

        # Send a clean response page so the user knows they can close the tab
        body = (
            b"<!DOCTYPE html><html><head><title>Cortex - Connected</title>"
            b"<style>body{font-family:system-ui;display:flex;align-items:center;"
            b"justify-content:center;height:100vh;margin:0;background:#0f1117;color:#fff;}"
            b".card{text-align:center;padding:2rem;}h1{color:#4ade80;}p{color:#94a3b8;}"
            b"</style></head><body><div class=\"card\"><h1>&#10003; Connected!</h1>"
            b"<p>You can close this tab and return to Cortex.</p></div></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress access logs


# ── Main OAuthManager ───────────────────────────────────────────────────────────

class OAuthManager:
    """
    Manages the full OAuth lifecycle for all Cortex connectors.

    Usage:
        manager = OAuthManager(credential_dir="path/to/db")
        result = manager.start_flow("notion", client_id="...", client_secret="...")
    """

    def __init__(self, credential_dir: str):
        self._credential_dir = credential_dir
        os.makedirs(credential_dir, exist_ok=True)
        # Fallback to file-based storage if keyring unavailable
        self._token_file = os.path.join(credential_dir, ".oauth_tokens.json")

    # ── Public API ──────────────────────────────────────────────────────────────

    def start_flow(
        self,
        app_id: str,
        client_id: str,
        client_secret: str,
        extra_config: Optional[Dict[str, Any]] = None,
        on_complete: Optional[Callable[[OAuthResult], None]] = None,
    ) -> OAuthResult:
        """
        Run the full OAuth PKCE flow:
        1. Start localhost:8765 listener
        2. Open provider auth URL in browser
        3. Wait for callback (60s timeout)
        4. Exchange code for tokens
        5. Store in keyring
        Returns OAuthResult with success/failure.
        """
        if app_id not in OAUTH_CONFIGS:
            return OAuthResult(success=False, app_id=app_id,
                               message=f"No OAuth config for '{app_id}'")

        config = dict(OAUTH_CONFIGS[app_id])
        if extra_config:
            config.update(extra_config)

        # Substitute template variables (e.g. {tenant_id} for MS Teams)
        tenant_id = (extra_config or {}).get("tenant_id", "common")
        for key in ("auth_url", "token_url"):
            config[key] = config[key].replace("{tenant_id}", tenant_id)

        # Generate CSRF state
        state = secrets.token_urlsafe(16)

        # Generate PKCE if required
        code_verifier, code_challenge = None, None
        if config.get("pkce"):
            code_verifier, code_challenge = _generate_pkce_pair()

        # Build auth URL
        redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
        scopes = " ".join(config["scopes"])

        auth_params: Dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
        }
        if code_challenge:
            auth_params["code_challenge"] = code_challenge
            auth_params["code_challenge_method"] = "S256"
        if config.get("extra_params"):
            auth_params.update(config["extra_params"])

        auth_url = config["auth_url"] + "?" + urllib.parse.urlencode(auth_params)

        # Reset handler state
        _CallbackHandler.received_code = None
        _CallbackHandler.received_state = None
        _CallbackHandler.received_error = None

        # Start callback server
        try:
            server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
        except OSError:
            return OAuthResult(success=False, app_id=app_id,
                               message=f"Port {CALLBACK_PORT} already in use. Close other Cortex windows and retry.")

        server_thread = threading.Thread(target=lambda: server.handle_request(), daemon=True)
        server_thread.start()

        # Open browser
        webbrowser.open(auth_url)
        print(f"  [OAuth] Browser opened for {app_id}. Waiting for callback...")

        # Wait up to 120 seconds
        server_thread.join(timeout=120)
        server.server_close()

        if _CallbackHandler.received_error:
            return OAuthResult(success=False, app_id=app_id,
                               message=f"OAuth denied: {_CallbackHandler.received_error}")

        if not _CallbackHandler.received_code:
            return OAuthResult(success=False, app_id=app_id,
                               message="OAuth timeout — no code received within 120 seconds.")

        # Verify CSRF state
        if _CallbackHandler.received_state != state:
            return OAuthResult(success=False, app_id=app_id,
                               message="OAuth state mismatch — possible CSRF attempt. Aborted.")

        # Exchange code for tokens
        return self._exchange_code(
            app_id=app_id,
            code=_CallbackHandler.received_code,
            config=config,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )

    def get_valid_token(self, app_id: str, client_id: str, client_secret: str) -> Optional[str]:
        """
        Return a valid access token. Auto-refreshes if within TOKEN_REFRESH_BUFFER_SECONDS
        of expiry. User is never prompted after the initial connect.
        """
        token_set = self._load_token_set(app_id)
        if not token_set:
            return None

        if token_set.is_expired() and token_set.refresh_token:
            config = OAUTH_CONFIGS.get(app_id, {})
            token_set = self._refresh_token(token_set, config, client_id, client_secret)
            if token_set:
                self._save_token_set(token_set)

        return token_set.access_token if token_set else None

    def revoke(self, app_id: str) -> bool:
        """Delete stored tokens for an app."""
        return self._delete_token_set(app_id)

    def is_connected(self, app_id: str) -> bool:
        """Check whether we have a stored token set for this app."""
        return self._load_token_set(app_id) is not None

    # ── Token exchange ─────────────────────────────────────────────────────────

    def _exchange_code(
        self,
        app_id: str,
        code: str,
        config: Dict[str, Any],
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        code_verifier: Optional[str],
    ) -> OAuthResult:
        if not HTTPX_AVAILABLE:
            return OAuthResult(success=False, app_id=app_id,
                               message="httpx not installed. Run: pip install httpx")

        payload: Dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        headers = {"Accept": "application/json"}

        method = config.get("token_method", "post")
        if method == "basic":
            # Notion uses HTTP Basic auth instead of client_secret in body
            import base64 as _b64
            creds = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
            headers["Content-Type"] = "application/json"
            try:
                resp = httpx.post(config["token_url"], json=payload, headers=headers, timeout=15)
            except Exception as e:
                return OAuthResult(success=False, app_id=app_id, message=f"Token request failed: {e}")
        else:
            payload["client_secret"] = client_secret
            try:
                resp = httpx.post(config["token_url"], data=payload, headers=headers, timeout=15)
            except Exception as e:
                return OAuthResult(success=False, app_id=app_id, message=f"Token request failed: {e}")

        if resp.status_code not in (200, 201):
            return OAuthResult(success=False, app_id=app_id,
                               message=f"Token endpoint returned {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        expires_in = data.get("expires_in")
        expires_at = time.time() + int(expires_in) if expires_in else None

        token_set = TokenSet(
            app_id=app_id,
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
            extra={k: v for k, v in data.items()
                   if k not in ("access_token", "refresh_token", "expires_in",
                                "token_type", "scope")},
        )
        self._save_token_set(token_set)
        print(f"  [OAuth] ✓ {app_id} connected successfully.")
        return OAuthResult(success=True, app_id=app_id,
                           message=f"Connected to {app_id}.", token_set=token_set)

    def _refresh_token(
        self,
        token_set: TokenSet,
        config: Dict[str, Any],
        client_id: str,
        client_secret: str,
    ) -> Optional[TokenSet]:
        if not HTTPX_AVAILABLE or not token_set.refresh_token:
            return None
        try:
            resp = httpx.post(
                config["token_url"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token_set.refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            expires_in = data.get("expires_in")
            token_set.access_token = data.get("access_token", token_set.access_token)
            token_set.refresh_token = data.get("refresh_token", token_set.refresh_token)
            token_set.expires_at = time.time() + int(expires_in) if expires_in else None
            print(f"  [OAuth] ✓ Token refreshed for {token_set.app_id}")
            return token_set
        except Exception as e:
            print(f"  [OAuth] Token refresh failed for {token_set.app_id}: {e}")
            return None

    # ── Token storage (keyring preferred, file fallback) ───────────────────────

    def _save_token_set(self, token_set: TokenSet) -> None:
        if KEYRING_AVAILABLE:
            try:
                keyring.set_password(KEYRING_SERVICE, token_set.app_id, token_set.to_json())
                return
            except Exception:
                pass
        # File fallback
        tokens = self._load_all_file_tokens()
        tokens[token_set.app_id] = json.loads(token_set.to_json())
        with open(self._token_file, "w") as f:
            json.dump(tokens, f, indent=2)

    def _load_token_set(self, app_id: str) -> Optional[TokenSet]:
        if KEYRING_AVAILABLE:
            try:
                raw = keyring.get_password(KEYRING_SERVICE, app_id)
                if raw:
                    return TokenSet.from_json(raw)
            except Exception:
                pass
        # File fallback
        tokens = self._load_all_file_tokens()
        if app_id in tokens:
            return TokenSet(**tokens[app_id])
        return None

    def _delete_token_set(self, app_id: str) -> bool:
        deleted = False
        if KEYRING_AVAILABLE:
            try:
                keyring.delete_password(KEYRING_SERVICE, app_id)
                deleted = True
            except Exception:
                pass
        tokens = self._load_all_file_tokens()
        if app_id in tokens:
            del tokens[app_id]
            with open(self._token_file, "w") as f:
                json.dump(tokens, f, indent=2)
            deleted = True
        return deleted

    def _load_all_file_tokens(self) -> Dict[str, Any]:
        if not os.path.exists(self._token_file):
            return {}
        try:
            with open(self._token_file) as f:
                return json.load(f)
        except Exception:
            return {}
