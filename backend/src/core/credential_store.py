"""
Credential Store — Secure credential management for app connectors.

Uses the OS-native credential store via the `keyring` library:
  - macOS:   Keychain
  - Linux:   SecretService / KWallet
  - Windows: Windows Credential Locker

Falls back to base64-obfuscated JSON file if keyring is unavailable
(e.g., headless servers without a desktop session).
"""

import os
import json
import base64
from typing import Dict, Optional, List

# Track which fields each connector stores (for enumeration)
_FIELDS_REGISTRY_KEY = "bhrm:fields"
_SERVICE_NAME = "bhrm-intelligence"

# Try to import keyring for secure storage
try:
    import keyring
    _KEYRING_AVAILABLE = True
except ImportError:
    _KEYRING_AVAILABLE = False


class CredentialStore:
    """Manages persistent storage of connector credentials.

    Priority order:
      1. OS keychain via `keyring` (encrypted at rest)
      2. JSON file with base64 obfuscation (legacy fallback)
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.store_path = os.path.join(db_path, ".credentials.json")
        self.use_keyring = _KEYRING_AVAILABLE
        self._ensure_state()

        # Auto-migrate legacy credentials to keyring on first use
        if self.use_keyring:
            self._migrate_legacy_to_keyring()

    def _ensure_state(self):
        """Ensure the state directory and fields registry exist."""
        os.makedirs(self.db_path, exist_ok=True)
        fields_file = os.path.join(self.db_path, ".credential_fields.json")
        if not os.path.exists(fields_file):
            with open(fields_file, "w") as f:
                json.dump({}, f)

    # ─── Public API ──────────────────────────────────────────────

    def save_credentials(self, app_id: str, credentials: Dict[str, str]):
        """Save credentials for a connector using the best available backend."""
        if self.use_keyring:
            self._keyring_save(app_id, credentials)
        else:
            self._legacy_save(app_id, credentials)

        # Record which fields this app stores (for enumeration/masking)
        self._save_field_names(app_id, list(credentials.keys()))

    def get_credentials(self, app_id: str) -> Optional[Dict[str, str]]:
        """Retrieve credentials for a connector."""
        fields = self._get_field_names(app_id)
        if not fields:
            # Try legacy store as fallback
            return self._legacy_get(app_id)

        if self.use_keyring:
            return self._keyring_get(app_id, fields)
        return self._legacy_get(app_id)

    def delete_credentials(self, app_id: str):
        """Remove credentials for a connector from all backends."""
        fields = self._get_field_names(app_id)

        # Remove from keyring
        if self.use_keyring and fields:
            for field in fields:
                try:
                    keyring.delete_password(_SERVICE_NAME, f"{app_id}:{field}")
                except Exception:
                    pass

        # Remove from legacy store
        self._legacy_delete(app_id)

        # Remove field registry
        self._delete_field_names(app_id)

    def has_credentials(self, app_id: str) -> bool:
        """Check if credentials exist for a connector."""
        creds = self.get_credentials(app_id)
        return creds is not None and bool(creds)

    def mask_credentials(self, app_id: str) -> Optional[Dict[str, str]]:
        """Return credentials with values masked (for frontend display)."""
        creds = self.get_credentials(app_id)
        if not creds:
            return None
        masked = {}
        for k, v in creds.items():
            if len(v) > 8:
                masked[k] = v[:4] + "****" + v[-4:]
            else:
                masked[k] = "****"
        return masked

    # ─── Keyring backend ─────────────────────────────────────────

    def _keyring_save(self, app_id: str, credentials: Dict[str, str]):
        """Store each credential field as a separate keyring entry."""
        for key, value in credentials.items():
            keyring.set_password(_SERVICE_NAME, f"{app_id}:{key}", value)

    def _keyring_get(self, app_id: str, fields: List[str]) -> Optional[Dict[str, str]]:
        """Retrieve credential fields from keyring."""
        result = {}
        for field in fields:
            try:
                value = keyring.get_password(_SERVICE_NAME, f"{app_id}:{field}")
                if value is not None:
                    result[field] = value
            except Exception:
                pass
        return result if result else None

    # ─── Legacy JSON+base64 backend ──────────────────────────────

    def _legacy_save(self, app_id: str, credentials: Dict[str, str]):
        """Save credentials with base64 obfuscation (legacy fallback)."""
        self._ensure_legacy_file()
        data = self._legacy_load_raw()
        data[app_id] = {k: self._encode(v) for k, v in credentials.items()}
        with open(self.store_path, "w") as f:
            json.dump(data, f, indent=2)

    def _legacy_get(self, app_id: str) -> Optional[Dict[str, str]]:
        """Retrieve credentials from the legacy JSON store."""
        data = self._legacy_load_raw()
        if app_id not in data:
            return None
        try:
            return {k: self._decode(v) for k, v in data[app_id].items()}
        except Exception:
            return None

    def _legacy_delete(self, app_id: str):
        """Remove credentials from the legacy JSON store."""
        self._ensure_legacy_file()
        data = self._legacy_load_raw()
        if app_id in data:
            del data[app_id]
            with open(self.store_path, "w") as f:
                json.dump(data, f, indent=2)

    def _legacy_load_raw(self) -> Dict:
        """Load raw data from the legacy JSON file."""
        if not os.path.exists(self.store_path):
            return {}
        try:
            with open(self.store_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _ensure_legacy_file(self):
        """Ensure the legacy JSON file exists."""
        if not os.path.exists(self.store_path):
            with open(self.store_path, "w") as f:
                json.dump({}, f)

    # ─── Field name registry ─────────────────────────────────────

    def _fields_path(self) -> str:
        return os.path.join(self.db_path, ".credential_fields.json")

    def _save_field_names(self, app_id: str, fields: List[str]):
        """Remember which fields an app stores (needed for keyring enumeration)."""
        path = self._fields_path()
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            data = {}
        data[app_id] = fields
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _get_field_names(self, app_id: str) -> List[str]:
        """Get the list of credential fields for an app."""
        path = self._fields_path()
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return data.get(app_id, [])
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _delete_field_names(self, app_id: str):
        """Remove field registry for an app."""
        path = self._fields_path()
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if app_id in data:
                del data[app_id]
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    # ─── Migration helper ────────────────────────────────────────

    def _migrate_legacy_to_keyring(self):
        """One-time migration of legacy base64 credentials to keyring."""
        data = self._legacy_load_raw()
        if not data:
            return

        migrated = 0
        for app_id, creds in data.items():
            try:
                decoded = {k: self._decode(v) for k, v in creds.items()}
                self._keyring_save(app_id, decoded)
                self._save_field_names(app_id, list(decoded.keys()))
                migrated += 1
            except Exception:
                pass  # Keep legacy entry if migration fails

        if migrated > 0:
            # Clear legacy file after successful migration
            backup = self.store_path + ".migrated.bak"
            os.rename(self.store_path, backup)
            with open(self.store_path, "w") as f:
                json.dump({}, f)

    # ─── Encoding helpers ────────────────────────────────────────

    @staticmethod
    def _encode(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("utf-8")

    @staticmethod
    def _decode(value: str) -> str:
        return base64.b64decode(value.encode("utf-8")).decode("utf-8")
