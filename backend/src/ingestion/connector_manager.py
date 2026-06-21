"""
Connector Manager — Central brain for all app integrations.

Manages:
  - Connection state (which apps are connected)
  - Credential validation (test before saving)
  - Ingestion dispatch (call the right connector when pipeline runs)
  - Persistent state via connector_state.json + credential_store
"""

import os
import json
import glob
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
from datetime import datetime, timezone

from core.credential_store import CredentialStore
from core.models import KnowledgeChunk


# ── Connector definitions ───────────────────────────────────────

CONNECTOR_REGISTRY = {
    # ── Tier 1: Zero-credential (auto-detect or gh CLI) ────────────────
    "localfolder": {
        "tier": 1,
        "name": "Local Folder",
        "description": "Any folder on your disk — scans .md, .json, .txt files",
        "fields": [
            {"key": "path", "label": "Folder Path", "type": "path", "required": True,
             "help": "e.g. /Users/yourname/Documents/company-docs"}
        ],
    },
    "obsidian": {
        "tier": 1,
        "name": "Obsidian",
        "description": "Auto-detects your Obsidian vault and ingests all notes",
        "fields": [],
    },
    "github": {
        "tier": 1,
        "name": "GitHub",
        "description": "Uses your gh CLI auth to pull repos, READMEs, issues, PRs",
        "fields": [],
    },

    # ── Tier 2: Token-based (paste key, works immediately) ─────────────
    "notion": {
        "tier": 2,
        "name": "Notion",
        "description": "Pages, databases, and docs from your Notion workspace",
        "fields": [
            {"key": "api_key", "label": "Integration Token", "type": "password", "required": True,
             "help": "notion.so/my-integrations → New integration → copy Internal Integration Secret (secret_...)"},
            {"key": "page_ids", "label": "Page / Database IDs (optional)", "type": "text", "required": False,
             "help": "Leave blank to sync all shared pages. Or paste comma-separated 32-char IDs from page URLs."},
        ],
        "oauth": False,
    },
    "slack": {
        "tier": 2,
        "name": "Slack",
        "description": "Channel messages and threads from your Slack workspace",
        "fields": [
            {"key": "bot_token", "label": "Bot User OAuth Token", "type": "password", "required": True,
             "help": "api.slack.com/apps → Your App → OAuth & Permissions → Bot User OAuth Token (xoxb-...)"},
            {"key": "channel_ids", "label": "Channel IDs (optional)", "type": "text", "required": False,
             "help": "Right-click channel → View channel details → copy ID. Leave blank for all public channels."},
        ],
    },
    "google_drive": {
        "tier": 2,
        "name": "Google Drive",
        "description": "Docs, Sheets, and Slides from your Google Drive",
        "fields": [
            {"key": "access_token", "label": "OAuth Access Token", "type": "password", "required": True,
             "help": "Get via: gcloud auth print-access-token  OR  use the OAuth flow below"},
            {"key": "folder_ids", "label": "Folder IDs (optional)", "type": "text", "required": False,
             "help": "Paste comma-separated folder IDs from Drive URLs. Leave blank to search all Drive."},
        ],
    },
    "confluence": {
        "tier": 2,
        "name": "Confluence",
        "description": "Wiki pages and spaces from Atlassian Confluence",
        "fields": [
            {"key": "access_token", "label": "Atlassian API Token", "type": "password", "required": True,
             "help": "id.atlassian.com/manage-profile/security/api-tokens → Create API token"},
            {"key": "base_url", "label": "Confluence Base URL", "type": "text", "required": True,
             "help": "e.g. https://yourcompany.atlassian.net/wiki"},
            {"key": "space_keys", "label": "Space Keys (optional)", "type": "text", "required": False,
             "help": "Comma-separated space keys e.g. ENG,DOCS. Leave blank for all spaces."},
        ],
    },
    "jira": {
        "tier": 2,
        "name": "Jira",
        "description": "Issues, epics, and changelogs from Jira",
        "fields": [
            {"key": "access_token", "label": "Atlassian API Token", "type": "password", "required": True,
             "help": "id.atlassian.com/manage-profile/security/api-tokens → Create API token"},
            {"key": "base_url", "label": "Jira Base URL", "type": "text", "required": True,
             "help": "e.g. https://yourcompany.atlassian.net"},
            {"key": "project_keys", "label": "Project Keys (optional)", "type": "text", "required": False,
             "help": "Comma-separated e.g. ENG,OPS. Leave blank for all projects."},
        ],
    },
    "linear": {
        "tier": 2,
        "name": "Linear",
        "description": "Issues, projects, and cycles from Linear",
        "fields": [
            {"key": "access_token", "label": "Personal API Key", "type": "password", "required": True,
             "help": "linear.app/settings/api → Personal API Keys → Create key"},
            {"key": "team_ids", "label": "Team IDs (optional)", "type": "text", "required": False,
             "help": "Leave blank to sync all teams you have access to."},
        ],
    },
    "ms_teams": {
        "tier": 2,
        "name": "Microsoft Teams",
        "description": "Channel messages and chats from Microsoft Teams",
        "fields": [
            {"key": "access_token", "label": "Azure AD Access Token", "type": "password", "required": True,
             "help": "portal.azure.com → App registrations → your app → get token via OAuth2"},
            {"key": "team_ids", "label": "Team IDs (optional)", "type": "text", "required": False,
             "help": "Leave blank to sync all teams. Paste comma-separated Team IDs from Teams admin."},
        ],
    },
    "whatsapp": {
        "tier": 2,
        "name": "WhatsApp Business",
        "description": "Incoming messages via WhatsApp Business Cloud API webhook",
        "fields": [
            {"key": "access_token", "label": "WhatsApp System Token", "type": "password", "required": True,
             "help": "developers.facebook.com → your app → WhatsApp → API Setup → System User Token"},
            {"key": "phone_id", "label": "Phone Number ID", "type": "text", "required": True,
             "help": "From WhatsApp Business API setup page — the numeric Phone Number ID"},
            {"key": "webhook_secret", "label": "Webhook Verify Token", "type": "text", "required": False,
             "help": "Any string you set in the Meta webhook config. Used to verify incoming messages."},
        ],
        "note": "Ingests incoming messages via webhook only. Historical backfill requires Business API access.",
    },
}



# Dedicated Connection Log
conn_logger = logging.getLogger("ConnectionLog")
if not conn_logger.handlers:
    conn_handler = logging.FileHandler("connection.log")
    conn_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    conn_logger.addHandler(conn_handler)
    conn_logger.setLevel(logging.INFO)

class ConnectorManager:
    """
    Manages the lifecycle of all app connectors:
    connect, disconnect, validate, ingest.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.state_path = os.path.join(db_path, "connector_state.json")
        self.cred_store = CredentialStore(db_path=db_path)
        # mcp_config.json lives next to the database dir (in backend/)
        self.mcp_config_path = os.path.join(os.path.dirname(db_path), "mcp_config.json")
        self._ensure_state_file()

    def _ensure_state_file(self):
        os.makedirs(self.db_path, exist_ok=True)
        if not os.path.exists(self.state_path):
            with open(self.state_path, "w") as f:
                json.dump({}, f)

    def _load_state(self) -> Dict:
        try:
            with open(self.state_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save_state(self, state: Dict):
        with open(self.state_path, "w") as f:
            json.dump(state, f, indent=2)

    def _load_mcp_config(self) -> Dict:
        """Load mcp_config.json (same format as Claude Desktop config)."""
        try:
            with open(self.mcp_config_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"mcpServers": {}}

    def _save_mcp_config(self, config: Dict):
        """Write mcp_config.json atomically."""
        with open(self.mcp_config_path, "w") as f:
            json.dump(config, f, indent=2)

    def _get_mcp_connector(self, app_id: str):
        """Return an MCPConnector if this app is configured in mcp_config.json."""
        try:
            from ingestion.mcp_client import MCPConnector
            config = self._load_mcp_config()
            return MCPConnector.from_config(app_id, config)
        except Exception:
            return None

    # ── Public API ───────────────────────────────────────────────

    def get_all_connectors(self) -> List[Dict[str, Any]]:
        """Return all connectors with their current connection status.
        
        Auto-detects pre-existing credentials from .env for Tier 2 connectors.
        If valid credentials exist but the connector isn't marked connected,
        auto-populates the state so the UI shows the correct status.
        """
        state = self._load_state()
        state_modified = False

        # Auto-detect credentials from .env for Tier 2 connectors
        ENV_AUTO_CONNECT = {
            "notion":       {"env_key": "NOTION_API_KEY",      "cred_field": "api_key"},
            "slack":        {"env_key": "SLACK_BOT_TOKEN",     "cred_field": "bot_token"},
            "google_drive": {"env_key": "GOOGLE_ACCESS_TOKEN", "cred_field": "access_token"},
            "confluence":   {"env_key": "CONFLUENCE_TOKEN",    "cred_field": "access_token"},
            "jira":         {"env_key": "JIRA_TOKEN",          "cred_field": "access_token"},
            "linear":       {"env_key": "LINEAR_TOKEN",        "cred_field": "access_token"},
            "ms_teams":     {"env_key": "MS_TEAMS_TOKEN",      "cred_field": "access_token"},
            "whatsapp":     {"env_key": "WHATSAPP_TOKEN",      "cred_field": "access_token"},
        }

        for app_id, env_info in ENV_AUTO_CONNECT.items():
            conn_state = state.get(app_id, {})
            if conn_state.get("connected"):
                continue  # Already connected — skip

            env_val = os.environ.get(env_info["env_key"], "")
            if env_val:
                # Credentials exist in .env but connector not marked connected — auto-connect
                state[app_id] = {
                    "connected": True,
                    "connected_at": datetime.now(timezone.utc).isoformat(),
                    "status_message": f"Auto-connected from .env ({env_info['env_key']})",
                }
                # Persist credentials to CredentialStore
                self.cred_store.save_credentials(app_id, {env_info["cred_field"]: env_val})
                state_modified = True
                print(f"  [ConnectorManager] Auto-connected {app_id} from .env ({env_info['env_key']})")

        if state_modified:
            self._save_state(state)

        result = []
        for app_id, info in CONNECTOR_REGISTRY.items():
            conn_state = state.get(app_id, {})
            entry = {
                "id": app_id,
                "name": info["name"],
                "description": info["description"],
                "tier": info["tier"],
                "fields": info["fields"],
                "connected": conn_state.get("connected", False),
                "connected_at": conn_state.get("connected_at"),
                "status_message": conn_state.get("status_message", ""),
                "masked_credentials": self.cred_store.mask_credentials(app_id),
            }
            # For tier 1, add detection info
            if app_id == "obsidian":
                entry["detected_path"] = self._detect_obsidian_vault()
            elif app_id == "github":
                entry["cli_available"] = self._check_gh_cli()
            result.append(entry)
        return result

    def connect(self, app_id: str, credentials: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Connect an app. For Tier 1, auto-detects. For Tier 2, validates credentials.
        Returns {status: "connected"|"error", message: str}
        """
        if app_id not in CONNECTOR_REGISTRY:
            return {"status": "error", "message": f"Unknown connector: {app_id}"}

        info = CONNECTOR_REGISTRY[app_id]

        # Tier 1: auto-detect
        if info["tier"] == 1:
            return self._connect_tier1(app_id, credentials)

        # Tier 2: validate credentials
        if not credentials:
            return {"status": "error", "message": f"Credentials required for {info['name']}"}

        # Check required fields
        for field in info["fields"]:
            if field["required"] and not credentials.get(field["key"]):
                return {"status": "error", "message": f"Missing required field: {field['label']}"}

        return self._connect_tier2(app_id, credentials)

    def disconnect(self, app_id: str) -> Dict[str, Any]:
        """Disconnect an app and remove its credentials."""
        state = self._load_state()
        if app_id in state:
            del state[app_id]
        self._save_state(state)
        self.cred_store.delete_credentials(app_id)
        return {"status": "disconnected", "message": f"{CONNECTOR_REGISTRY.get(app_id, {}).get('name', app_id)} disconnected."}

    def test_connection(self, app_id: str, credentials: Dict[str, str]) -> Dict[str, Any]:
        """Validate credentials without saving them."""
        if app_id not in CONNECTOR_REGISTRY:
            return {"status": "error", "message": f"Unknown connector: {app_id}"}

        valid, message = self._validate_credentials(app_id, credentials)
        return {"status": "valid" if valid else "invalid", "message": message}

    def ingest_all_connected(self) -> List[KnowledgeChunk]:
        """
        Called by the pipeline during Layer 1.
        Iterates all connected apps and calls their ingestion functions.
        Returns combined list of raw KnowledgeChunks.
        """
        state = self._load_state()
        all_chunks = []

        # .env key mapping for credential fallback
        ENV_CRED_MAP = {
            "notion": {"api_key": "NOTION_API_KEY"},
            "slack": {"bot_token": "SLACK_BOT_TOKEN"},
            "google_drive": {"access_token": "GOOGLE_ACCESS_TOKEN"},
            "confluence": {"access_token": "CONFLUENCE_TOKEN"},
            "jira": {"access_token": "JIRA_TOKEN"},
            "linear": {"access_token": "LINEAR_TOKEN"},
            "ms_teams": {"access_token": "MS_TEAMS_TOKEN"},
            "whatsapp": {"access_token": "WHATSAPP_TOKEN"},
        }

        for app_id, conn_state in state.items():
            if not conn_state.get("connected"):
                continue

            app_name = CONNECTOR_REGISTRY.get(app_id, {}).get("name", app_id)
            print(f"\n  [ConnectorManager] Ingesting from: {app_name}")

            # Ensure credentials are available (fallback to .env)
            if app_id in ENV_CRED_MAP and not self.cred_store.get_credentials(app_id):
                env_map = ENV_CRED_MAP[app_id]
                env_creds = {}
                for cred_key, env_var in env_map.items():
                    val = os.environ.get(env_var, "")
                    if val:
                        env_creds[cred_key] = val
                if env_creds:
                    self.cred_store.save_credentials(app_id, env_creds)
                    print(f"  [ConnectorManager] Loaded {app_id} credentials from .env")

            try:
                chunks = self._dispatch_ingestion(app_id, conn_state)
                if chunks:
                    all_chunks.extend(chunks)
                    print(f"  [ConnectorManager] → {len(chunks)} chunks from {app_id}")
                else:
                    print(f"  [ConnectorManager] ⚠ {app_name} returned 0 chunks — check credentials and permissions")
            except Exception as e:
                print(f"  [ConnectorManager] ERROR ingesting {app_id}: {e}")

        return all_chunks

    def ingest_single(self, app_id: str) -> List[KnowledgeChunk]:
        """
        Ingest data from a single connected app (used for per-connector sync).
        Returns list of KnowledgeChunks from just that app.
        """
        state = self._load_state()
        conn_state = state.get(app_id, {})
        if not conn_state.get("connected"):
            print(f"  [ConnectorManager] {app_id} not connected — skipping ingest")
            return []

        # .env key mapping for credential fallback
        ENV_CRED_MAP = {
            "notion": {"api_key": "NOTION_API_KEY"},
            "slack": {"bot_token": "SLACK_BOT_TOKEN"},
            "google_drive": {"access_token": "GOOGLE_ACCESS_TOKEN"},
            "confluence": {"access_token": "CONFLUENCE_TOKEN"},
            "jira": {"access_token": "JIRA_TOKEN"},
            "linear": {"access_token": "LINEAR_TOKEN"},
            "ms_teams": {"access_token": "MS_TEAMS_TOKEN"},
            "whatsapp": {"access_token": "WHATSAPP_TOKEN"},
        }

        # Ensure credentials are available (fallback to .env)
        if app_id in ENV_CRED_MAP and not self.cred_store.get_credentials(app_id):
            env_map = ENV_CRED_MAP[app_id]
            env_creds = {}
            for cred_key, env_var in env_map.items():
                val = os.environ.get(env_var, "")
                if val:
                    env_creds[cred_key] = val
            if env_creds:
                self.cred_store.save_credentials(app_id, env_creds)
                print(f"  [ConnectorManager] Loaded {app_id} credentials from .env")

        app_name = CONNECTOR_REGISTRY.get(app_id, {}).get("name", app_id)
        print(f"\n  [ConnectorManager] Ingesting from: {app_name}")

        try:
            chunks = self._dispatch_ingestion(app_id, conn_state)
            if chunks:
                print(f"  [ConnectorManager] → {len(chunks)} chunks from {app_id}")
            else:
                print(f"  [ConnectorManager] ⚠ {app_name} returned 0 chunks")
            return chunks or []
        except Exception as e:
            print(f"  [ConnectorManager] ERROR ingesting {app_id}: {e}")
            return []

    def get_connected_data_paths(self) -> List[str]:
        """Return filesystem paths from connected local connectors (for backward compat)."""
        state = self._load_state()
        paths = []
        for app_id, conn_state in state.items():
            if not conn_state.get("connected"):
                continue
            path = conn_state.get("path")
            if path and os.path.isdir(path):
                paths.append(path)
        return paths

    # ── Tier 1 connect logic ─────────────────────────────────────

    def _connect_tier1(self, app_id: str, credentials: Optional[Dict] = None) -> Dict[str, Any]:
        state = self._load_state()

        if app_id == "localfolder":
            path = (credentials or {}).get("path", "")
            if not path:
                return {"status": "error", "message": "Please provide a folder path."}
            if not os.path.isdir(path):
                return {"status": "error", "message": f"Folder not found: {path}"}
            state[app_id] = {
                "connected": True,
                "path": path,
                "connected_at": datetime.now(timezone.utc).isoformat(),
                "status_message": f"Connected to {path}",
            }
            self._save_state(state)
            conn_logger.info(f"Connected to local folder: {path}")
            return {"status": "connected", "message": f"Connected to folder: {path}"}

        elif app_id == "obsidian":
            vault_path = self._detect_obsidian_vault()
            if not vault_path:
                # Allow manual path if auto-detect fails
                manual = (credentials or {}).get("path", "")
                if manual and os.path.isdir(manual):
                    vault_path = manual
                else:
                    return {"status": "error", "message": "No Obsidian vault found. Check that Obsidian is installed."}
            state[app_id] = {
                "connected": True,
                "path": vault_path,
                "connected_at": datetime.now(timezone.utc).isoformat(),
                "status_message": f"Vault: {vault_path}",
            }
            self._save_state(state)
            conn_logger.info(f"Connected to Obsidian vault: {vault_path}")
            return {"status": "connected", "message": f"Connected to Obsidian vault: {vault_path}"}

        elif app_id == "github":
            if not self._check_gh_cli():
                return {"status": "error", "message": "GitHub CLI (gh) not found or not authenticated. Install it with: brew install gh && gh auth login"}
            from ingestion.github_connector import GitHubConnector
            gh = GitHubConnector()
            user = gh.get_auth_user() or "unknown"
            state[app_id] = {
                "connected": True,
                "github_user": user,
                "connected_at": datetime.now(timezone.utc).isoformat(),
                "status_message": f"Authenticated as {user}",
            }
            self._save_state(state)
            conn_logger.info(f"Connected to GitHub as {user} via CLI")
            return {"status": "connected", "message": f"Connected to GitHub as {user}"}

        return {"status": "error", "message": f"Unknown tier 1 connector: {app_id}"}

    # ── Tier 2 connect logic ─────────────────────────────────────

    def _connect_tier2(self, app_id: str, credentials: Dict[str, str]) -> Dict[str, Any]:
        # Validate first
        valid, message = self._validate_credentials(app_id, credentials)
        if not valid:
            return {"status": "error", "message": message}

        # Save credentials to keyring/JSON store
        self.cred_store.save_credentials(app_id, credentials)

        # ── Root fix: also write env var so all code paths see the token ──
        env_file = os.path.join(os.path.dirname(self.db_path), ".env")
        env_key_map = {
            "notion": ("api_key", "NOTION_API_KEY"),
            "slack": ("bot_token", "SLACK_BOT_TOKEN"),
            "google_drive": ("access_token", "GOOGLE_ACCESS_TOKEN"),
            "confluence": ("access_token", "CONFLUENCE_TOKEN"),
            "jira": ("access_token", "JIRA_TOKEN"),
            "linear": ("access_token", "LINEAR_TOKEN"),
            "ms_teams": ("access_token", "MS_TEAMS_TOKEN"),
            "whatsapp": ("access_token", "WHATSAPP_TOKEN"),
        }
        if app_id in env_key_map:
            cred_key, env_name = env_key_map[app_id]
            token_val = credentials.get(cred_key, "")
            if token_val:
                # Write to .env
                existing = []
                if os.path.exists(env_file):
                    existing = [l for l in open(env_file).readlines() if not l.startswith(f"{env_name}=")]
                existing.append(f"{env_name}={token_val}\n")
                with open(env_file, "w") as f: f.writelines(existing)
                # Also set live in process
                os.environ[env_name] = token_val

                # ── Also write to mcp_config.json so MCPConnector can use it ──
                mcp_cfg = self._load_mcp_config()
                servers = mcp_cfg.setdefault("mcpServers", {})
                mcp_env_builders = {
                    "notion": lambda t: {
                        "command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"],
                        "env": {"OPENAPI_MCP_HEADERS": f'{{"Authorization": "Bearer {t}"}}'}
                    },
                    "slack": lambda t: {
                        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-slack"],
                        "env": {"SLACK_BOT_TOKEN": t}
                    },
                    "github": lambda t: {
                        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": t}
                    },
                    "linear": lambda t: {
                        "command": "npx", "args": ["-y", "@linear/linear-mcp-server"],
                        "env": {"LINEAR_API_KEY": t}
                    },
                    "google_drive": lambda _: {
                        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-gdrive"],
                        "env": {}
                    },
                }
                if app_id in mcp_env_builders:
                    servers[app_id] = mcp_env_builders[app_id](token_val)
                    self._save_mcp_config(mcp_cfg)
                    print(f"  [ConnectorManager] mcp_config.json updated for {app_id} ✓")

        # Update connector state
        state = self._load_state()
        state[app_id] = {
            "connected": True,
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "status_message": message,
        }
        self._save_state(state)

        name = CONNECTOR_REGISTRY[app_id]["name"]
        conn_logger.info(f"Connected to {name} ({app_id}): {message}")
        return {"status": "connected", "message": f"{name} connected. {message}"}



    # ── Credential validation ────────────────────────────────────

    def _validate_credentials(self, app_id: str, credentials: Dict[str, str]) -> tuple:
        """Returns (is_valid: bool, message: str)"""

        if app_id == "notion":
            api_key = credentials.get("api_key", "")
            if not api_key.startswith(("ntn_", "secret_")):
                return False, "Invalid Notion token format. Should start with 'ntn_' or 'secret_'."
            try:
                from notion_client import Client
                client = Client(auth=api_key)
                client.users.me()
                return True, "Notion token validated successfully."
            except ImportError:
                return True, "Token format looks valid (notion-client not installed for full validation)."
            except Exception as e:
                return False, f"Notion API error: {str(e)}"

        elif app_id == "slack":
            bot_token = credentials.get("bot_token", "")
            if not bot_token.startswith("xoxb-"):
                return False, "Invalid Slack token format. Should start with 'xoxb-'."
            try:
                from slack_sdk import WebClient
                client = WebClient(token=bot_token)
                response = client.auth_test()
                team = response.get("team", "unknown")
                return True, f"Connected to workspace: {team}"
            except ImportError:
                return True, "Token format looks valid (slack_sdk not installed for full validation)."
            except Exception as e:
                return False, f"Slack API error: {str(e)}"

        elif app_id == "google_drive":
            token = credentials.get("access_token", "")
            if len(token) < 20:
                return False, "Access token looks too short. Paste the full OAuth token."
            return True, "Token accepted. Will validate on first sync."

        elif app_id == "confluence":
            token = credentials.get("access_token", "")
            base_url = credentials.get("base_url", "")
            if not token:
                return False, "API token required."
            if not base_url.startswith("http"):
                return False, "Base URL must start with https://"
            return True, f"Credentials accepted for {base_url}."

        elif app_id == "jira":
            token = credentials.get("access_token", "")
            base_url = credentials.get("base_url", "")
            if not token:
                return False, "API token required."
            if not base_url.startswith("http"):
                return False, "Base URL must start with https://"
            return True, f"Credentials accepted for {base_url}."

        elif app_id == "linear":
            token = credentials.get("access_token", "")
            if len(token) < 10:
                return False, "API key looks too short."
            return True, "Linear API key accepted."

        elif app_id == "ms_teams":
            token = credentials.get("access_token", "")
            if len(token) < 20:
                return False, "Access token looks too short."
            return True, "Teams token accepted. Will validate on first sync."

        elif app_id == "whatsapp":
            token = credentials.get("access_token", "")
            phone_id = credentials.get("phone_id", "")
            if not token:
                return False, "System token required."
            if not phone_id:
                return False, "Phone Number ID required."
            return True, "WhatsApp credentials accepted. Configure webhook URL to receive messages."

        # Unknown connector — accept as-is
        return True, "Accepted."

    # ── Ingestion dispatch ───────────────────────────────────────

    def _dispatch_ingestion(self, app_id: str, conn_state: Dict) -> List[KnowledgeChunk]:
        """
        Route to the correct ingestion function based on app_id.

        Priority:
          1. Official MCP server (if configured in mcp_config.json) ← preferred
          2. Custom connector code (fallback)
        """
        # ── MCP-first: use official MCP server if configured ─────────
        MCP_SUPPORTED = {"notion", "slack", "github", "google_drive", "linear"}
        if app_id in MCP_SUPPORTED:
            mcp_conn = self._get_mcp_connector(app_id)
            if mcp_conn is not None:
                try:
                    _mcp_chunks = self._dispatch_via_mcp(app_id, mcp_conn, conn_state)
                    if _mcp_chunks:  # Only use MCP if it actually returned data
                        return _mcp_chunks
                    print(f"  [MCP] {app_id}: 0 chunks via MCP, using direct connector instead")
                except Exception as e:
                    print(f"  [MCP] {app_id} failed, falling back to custom: {e}")

        # ── Custom connector fallback ─────────────────────────────────

        if app_id == "localfolder":
            path = conn_state.get("path")
            if path and os.path.isdir(path):
                from ingestion.parsers import LocalFileParser
                parser = LocalFileParser(raw_data_dir=path)
                return parser.ingest_directory()

        elif app_id == "obsidian":
            path = conn_state.get("path")
            if path and os.path.isdir(path):
                from ingestion.parsers import LocalFileParser
                parser = LocalFileParser(raw_data_dir=path)
                return parser.ingest_directory()

        elif app_id == "github":
            from ingestion.github_connector import GitHubConnector
            gh = GitHubConnector()
            return gh.ingest(limit_repos=5)

        elif app_id == "notion":

            creds = self.cred_store.get_credentials("notion")
            if creds:
                from ingestion.notion_connector import NotionConnector
                import uuid as _uuid
                from core.models import (
                    KnowledgeChunk, KnowledgeMetadata, KnowledgeType,
                    SourceType, Department, ProcessingLayer
                )

                connector = NotionConnector(token=creds.get("api_key"))

                # Delta sync: only fetch pages edited since last successful sync
                since = creds.get("sync_cursor") or None
                known_ids = set(creds.get("known_page_ids", []))
                
                if since:
                    print(f"  [Notion] Delta sync — fetching pages edited since {since}")
                else:
                    print("  [Notion] Full sync — no cursor found, fetching all pages...")
                try:
                    pages = connector.fetch_workspace(since=since, known_ids=known_ids)
                except Exception as _we:
                    print(f"  [Notion] fetch_workspace error: {_we}")
                    pages = []

                if not pages:
                    if since:
                        print("  [Notion] No pages changed since last sync — nothing to ingest.")
                    else:
                        print("  [Notion] No pages returned — share pages with your integration.")
                    return []

                # Infer department from Notion page content keywords
                def _infer_dept(title: str, content: str) -> Department:
                    text = (title + " " + content[:500]).lower()
                    if any(w in text for w in ["engineer", "deploy", "code", "pr", "github", "api", "bug", "sprint",
                                                "docker", "kubernetes", "ci/cd", "pipeline", "server", "debug"]):
                        return Department.ENGINEERING
                    if any(w in text for w in ["market", "brand", "campaign", "content", "post", "linkedin", "seo",
                                                "audience", "analytics", "copywriting", "creative"]):
                        return Department.MARKETING
                    if any(w in text for w in ["sales", "deal", "revenue", "lead", "crm", "customer",
                                                "prospect", "quota", "commission", "pricing"]):
                        return Department.SALES
                    if any(w in text for w in ["ops", "operation", "process", "policy", "workflow",
                                                "onboarding", "compliance", "vendor", "procurement"]):
                        return Department.OPS
                    return Department.SHARED

                # Infer knowledge type from content analysis
                def _infer_knowledge_type(title: str, content: str) -> KnowledgeType:
                    text = (title + " " + content[:500]).lower()
                    if any(w in text for w in ["security", "vulnerab", "auth", "permission", "access control", "encrypt"]):
                        return KnowledgeType.SECURITY_RULE
                    if any(w in text for w in ["never", "do not", "don't", "avoid", "incident", "outage", "postmortem"]):
                        return KnowledgeType.FAILURE_PATTERN
                    if any(w in text for w in ["edge case", "exception", "unless", "special case", "corner case"]):
                        return KnowledgeType.EDGE_CASE
                    if any(w in text for w in ["policy", "compliance", "regulation", "gdpr", "legal", "terms"]):
                        return KnowledgeType.POLICY
                    if any(w in text for w in ["approval", "approve", "sign off", "authorize", "review required"]):
                        return KnowledgeType.APPROVAL_FLOW
                    if any(w in text for w in ["escalat", "notify", "alert", "page", "on-call", "emergency"]):
                        return KnowledgeType.ESCALATION
                    if any(w in text for w in ["tool", "workflow", "setup", "configure", "integration", "terraform"]):
                        return KnowledgeType.TOOL_WORKFLOW
                    if any(w in text for w in ["glossary", "means", "defined as", "refers to", "aka", "acronym"]):
                        return KnowledgeType.GLOSSARY
                    if any(w in text for w in ["decision", "decided", "chose", "selected", "going with"]):
                        return KnowledgeType.DECISION
                    if any(w in text for w in ["prefer", "preference", "usually", "we like", "standard"]):
                        return KnowledgeType.PREFERENCE
                    # Default: SOP (standard operating procedure)
                    return KnowledgeType.SOP

                chunks = []
                total_words = 0
                for page in pages:
                    content = page.get("content", "").strip()
                    if not content:
                        continue
                    title = page.get("title") or f"Notion {page.get('type', 'page').title()}"
                    page_id = page.get("id", str(_uuid.uuid4()))

                    # Include page properties as additional context in the content
                    properties_text = page.get("properties", "")
                    if properties_text and properties_text not in content:
                        content = properties_text + "\n\n" + content

                    dept = _infer_dept(title, content)
                    k_type = _infer_knowledge_type(title, content)

                    # Use smart summary from connector, not dumb truncation
                    summary = page.get("summary", content[:200].replace("\n", " "))

                    # Extract meaningful tags from content
                    content_tags = ["notion", page.get("type", "page"), dept.value, k_type.value]
                    # Add tags from headings
                    for line in content.split("\n")[:20]:
                        if line.strip().startswith("#"):
                            tag = line.strip().lstrip("# ").lower().replace(" ", "-")[:30]
                            if tag and tag not in content_tags:
                                content_tags.append(tag)

                    word_count = page.get("word_count", len(content.split()))
                    total_words += word_count

                    chunk = KnowledgeChunk(
                        id=str(_uuid.uuid4()),
                        department=dept,
                        knowledge_type=k_type,
                        source_type=SourceType.NOTION,
                        source_identifier=f"notion:{page_id}",
                        title=title[:120],
                        content=content,
                        summary=summary,
                        tags=content_tags[:10],  # Cap at 10 tags
                        metadata=KnowledgeMetadata(confidence_score=0.9),
                        processing_layer=ProcessingLayer.RAW,
                    )
                    chunks.append(chunk)
                    print(f"  [Notion] Queued [{dept.value:10}] [{k_type.value:15}] {title[:50]} ({word_count}w)")

                print(f"  [Notion] Total: {len(chunks)} pages, {total_words} words of knowledge")

                # Persist sync cursor: max last_edited across fetched pages
                last_edited_times = [p.get("last_edited", "") for p in pages if p.get("last_edited")]
                if last_edited_times:
                    new_cursor = max(last_edited_times)
                    creds["sync_cursor"] = max(new_cursor, since) if since else new_cursor
                
                known_ids.update(p.get("id") for p in pages)
                creds["known_page_ids"] = list(known_ids)
                
                self.cred_store.save_credentials("notion", creds)
                if last_edited_times:
                    print(f"  [Notion] Sync cursor updated → {creds['sync_cursor']}")

                return chunks

        elif app_id == "slack":
            creds = self.cred_store.get_credentials("slack")
            if creds:
                from ingestion.slack_connector import SlackConnector
                connector = SlackConnector(bot_token=creds.get("bot_token"))

                raw_channels = (creds.get("channel_ids") or creds.get("channel_id") or "").strip()
                channel_ids = [c.strip() for c in raw_channels.split(",") if c.strip()]
                if not channel_ids:
                    channel_ids = ["C1234_ENGINEERING"]

                chunks = []
                import uuid as _uuid
                from core.models import (
                    KnowledgeChunk, KnowledgeMetadata, KnowledgeType,
                    SourceType, Department, ProcessingLayer
                )
                for channel_id in channel_ids:
                    try:
                        messages = connector.fetch_channel_history(channel_id=channel_id)
                        if not messages:
                            continue
                        combined = "\n".join(messages)
                        chunk = KnowledgeChunk(
                            id=str(_uuid.uuid4()),
                            department=Department.ENGINEERING,
                            knowledge_type=KnowledgeType.SOP,
                            source_type=SourceType.SLACK,
                            source_identifier=f"slack:{channel_id}",
                            title=f"Slack Channel {channel_id}",
                            content=combined,
                            summary=combined[:200].replace("\n", " "),
                            tags=["slack", channel_id],
                            metadata=KnowledgeMetadata(confidence_score=0.8),
                            processing_layer=ProcessingLayer.RAW,
                        )
                        chunks.append(chunk)
                    except Exception as e:
                        print(f"  [Slack] Failed to fetch channel {channel_id}: {e}")
                return chunks

        elif app_id == "google_drive":
            creds = self.cred_store.get_credentials("google_drive")
            if creds:
                try:
                    from ingestion.connectors.google_drive_connector import GoogleDriveConnector
                    connector = GoogleDriveConnector(access_token=creds.get("access_token"))
                    folder_ids = [f.strip() for f in (creds.get("folder_ids") or "").split(",") if f.strip()]
                    if folder_ids:
                        connector.set_folders(folder_ids)  # Bug 2 fix: use setter, not fetch_delta kwarg
                    since = creds.get("sync_cursor") or None
                    results = connector.fetch_delta(since=since)
                    if connector.last_sync_cursor:
                        creds["sync_cursor"] = connector.last_sync_cursor
                        self.cred_store.save_credentials("google_drive", creds)
                    return results
                except Exception as e:
                    print(f"  [GoogleDrive] Ingestion error: {e}")

        elif app_id == "confluence":
            creds = self.cred_store.get_credentials("confluence")
            if creds:
                try:
                    from ingestion.connectors.confluence_connector import ConfluenceConnector
                    connector = ConfluenceConnector(
                        access_token=creds.get("access_token"),
                        base_url=creds.get("base_url"),
                    )
                    space_keys = [s.strip() for s in (creds.get("space_keys") or "").split(",") if s.strip()]
                    if space_keys:
                        connector.set_spaces(space_keys)  # Bug 2 fix
                    since = creds.get("sync_cursor") or None
                    results = connector.fetch_delta(since=since)
                    if connector.last_sync_cursor:
                        creds["sync_cursor"] = connector.last_sync_cursor
                        self.cred_store.save_credentials("confluence", creds)
                    return results
                except Exception as e:
                    print(f"  [Confluence] Ingestion error: {e}")

        elif app_id == "jira":
            creds = self.cred_store.get_credentials("jira")
            if creds:
                try:
                    from ingestion.connectors.jira_connector import JiraConnector
                    connector = JiraConnector(
                        access_token=creds.get("access_token"),
                        base_url=creds.get("base_url"),
                    )
                    project_keys = [p.strip() for p in (creds.get("project_keys") or "").split(",") if p.strip()]
                    if project_keys:
                        connector.set_projects(project_keys)  # Bug 2 fix
                    since = creds.get("sync_cursor") or None
                    results = connector.fetch_delta(since=since)
                    if connector.last_sync_cursor:
                        creds["sync_cursor"] = connector.last_sync_cursor
                        self.cred_store.save_credentials("jira", creds)
                    return results
                except Exception as e:
                    print(f"  [Jira] Ingestion error: {e}")

        elif app_id == "linear":
            creds = self.cred_store.get_credentials("linear")
            if creds:
                try:
                    from ingestion.connectors.linear_connector import LinearConnector
                    connector = LinearConnector(access_token=creds.get("access_token"))
                    team_ids = [t.strip() for t in (creds.get("team_ids") or "").split(",") if t.strip()]
                    if team_ids:
                        connector.set_teams(team_ids)  # Bug 2 fix
                    since = creds.get("sync_cursor") or None
                    results = connector.fetch_delta(since=since)
                    if connector.last_sync_cursor:
                        creds["sync_cursor"] = connector.last_sync_cursor
                        self.cred_store.save_credentials("linear", creds)
                    return results
                except Exception as e:
                    print(f"  [Linear] Ingestion error: {e}")

        elif app_id == "ms_teams":
            creds = self.cred_store.get_credentials("ms_teams")
            if creds:
                try:
                    from ingestion.connectors.ms_teams_connector import MSTeamsConnector
                    connector = MSTeamsConnector(access_token=creds.get("access_token"))
                    team_ids = [t.strip() for t in (creds.get("team_ids") or "").split(",") if t.strip()]
                    if team_ids:
                        # set_channels expects [{team_id, channel_id}]; parse "team/channel" format
                        channel_pairs = [
                            {"team_id": p.split("/")[0], "channel_id": p.split("/")[1]}
                            for p in team_ids if "/" in p
                        ] or [{"team_id": t, "channel_id": ""} for t in team_ids]
                        connector.set_channels(channel_pairs)  # Bug 2 fix
                    since = creds.get("sync_cursor") or None
                    results = connector.fetch_delta(since=since)
                    if connector.last_sync_cursor:
                        creds["sync_cursor"] = connector.last_sync_cursor
                        self.cred_store.save_credentials("ms_teams", creds)
                    return results
                except Exception as e:
                    print(f"  [MSTeams] Ingestion error: {e}")

        elif app_id == "whatsapp":
            # WhatsApp is webhook-only — messages arrive via POST /webhooks/whatsapp
            # Nothing to pull here; return empty (webhook handler adds them to pipeline)
            print("  [WhatsApp] Webhook-mode connector — messages arrive via webhook, no pull needed.")
            return []

        return []

    # ── Detection helpers ────────────────────────────────────────

    def _dispatch_via_mcp(self, app_id: str, mcp_conn, conn_state: Dict) -> List[KnowledgeChunk]:
        """
        Call official MCP server tools and convert responses to KnowledgeChunks.
        Called when mcp_config.json has a server entry for this app_id.
        """
        import uuid as _uuid
        from ingestion.mcp_client import (
            ingest_via_mcp_notion, ingest_via_mcp_slack,
            ingest_via_mcp_github, ingest_via_mcp_linear,
        )
        from core.models import (
            KnowledgeChunk, KnowledgeMetadata, KnowledgeType,
            SourceType, Department, ProcessingLayer
        )

        SOURCE_TYPE_MAP = {
            "notion": SourceType.NOTION,
            "slack": SourceType.SLACK,
            "github": SourceType.GITHUB,
            "google_drive": SourceType.LOCAL_FILE,  # no GOOGLE_DRIVE type yet
            "linear": SourceType.LOCAL_FILE,
        }
        source_type = SOURCE_TYPE_MAP.get(app_id, SourceType.LOCAL_FILE)

        # Get raw text from MCP server
        raw_texts: list[str] = []
        creds = self.cred_store.get_credentials(app_id) or {}

        if app_id == "notion":
            raw_ids = (creds.get("page_ids") or "").strip()
            page_ids = [p.strip() for p in raw_ids.split(",") if p.strip()] or None
            raw_texts = ingest_via_mcp_notion(mcp_conn, page_ids)

        elif app_id == "slack":
            raw_ch = (creds.get("channel_ids") or "").strip()
            channel_ids = [c.strip() for c in raw_ch.split(",") if c.strip()] or None
            raw_texts = ingest_via_mcp_slack(mcp_conn, channel_ids)

        elif app_id == "github":
            raw_texts = ingest_via_mcp_github(mcp_conn)

        elif app_id == "linear":
            raw_ids = (creds.get("team_ids") or "").strip()
            team_ids = [t.strip() for t in raw_ids.split(",") if t.strip()] or None
            raw_texts = ingest_via_mcp_linear(mcp_conn, team_ids)

        # Convert to KnowledgeChunks
        chunks = []
        for i, text in enumerate(raw_texts):
            if not text.strip():
                continue
            chunk = KnowledgeChunk(
                id=str(_uuid.uuid4()),
                department=Department.SHARED,
                knowledge_type=KnowledgeType.SOP,
                source_type=source_type,
                source_identifier=f"{app_id}:mcp:{i}",
                title=f"{app_id.title()} (via MCP) — item {i+1}",
                content=text[:4000],  # cap at 4k chars
                summary=text[:200].replace("\n", " "),
                tags=[app_id, "mcp"],
                metadata=KnowledgeMetadata(confidence_score=0.85),
                processing_layer=ProcessingLayer.RAW,
            )
            chunks.append(chunk)

        print(f"  [MCP] {app_id}: {len(chunks)} chunks via official MCP server")
        return chunks

    # ── Detection helpers ────────────────────────────────────────

    @staticmethod
    def _detect_obsidian_vault() -> Optional[str]:

        """Auto-detect Obsidian vault by looking for .obsidian directories."""
        home = Path.home()
        # Common locations
        candidates = [
            home / "Documents",
            home / "Desktop",
            home / "obsidian-vault",
            home / "Obsidian",
            home / "Notes",
            home,
        ]
        for parent in candidates:
            if not parent.exists():
                continue
            # Check if this directory IS a vault
            if (parent / ".obsidian").exists():
                return str(parent)
            # Check immediate children
            try:
                for child in parent.iterdir():
                    if child.is_dir() and (child / ".obsidian").exists():
                        return str(child)
            except PermissionError:
                continue
        return None

    @staticmethod
    def _check_gh_cli() -> bool:
        """Check if GitHub CLI is installed and authenticated."""
        import subprocess
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
