"""
BaseConnector — Abstract interface all Cortex connectors must implement.

Enforces:
  - connect()         : validate credentials / complete OAuth
  - test_connection() : check connectivity without saving state
  - list_resources()  : enumerate channels / pages / folders user can select
  - fetch_delta()     : incremental fetch since a cursor (timestamp or token)
  - get_permalink()   : canonical URL back to the source document/message
  - last_sync_cursor  : position of last successful sync (persisted externally)

Principle of least privilege: connectors MUST NOT request write scopes.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime, timezone


# ── Value objects ───────────────────────────────────────────────────────────────

@dataclass
class ConnectResult:
    success: bool
    message: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Resource:
    """A selectable resource the user can choose to sync (channel, page, folder…)."""
    id: str                          # Provider-side ID
    name: str                        # Human-readable display name
    resource_type: str               # "channel" | "page" | "folder" | "repo" | "project"
    parent_id: Optional[str] = None  # For nested structures
    description: str = ""
    member_count: Optional[int] = None
    is_private: bool = False


@dataclass
class RawDocument:
    """
    A single piece of raw content returned by fetch_delta().
    The pipeline turns this into KnowledgeChunks downstream.
    """
    location_key: str        # Stable ID for PointerRecord: e.g. "channel_id/ts"
    permalink: str           # Direct URL back to the source
    title: str               # Best-effort human title
    content: str             # Raw text content
    content_hash: str        # SHA256(content)[:16] — used for hash-gating
    source_app: str          # "slack" | "notion" | "gdrive" | …
    modified_at: datetime    # When the source says this was last changed
    author: str = ""
    resource_id: str = ""    # Which resource (channel, page) this came from
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, *, location_key: str, permalink: str, title: str,
              content: str, source_app: str, modified_at: datetime,
              **kwargs) -> "RawDocument":
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        return cls(
            location_key=location_key,
            permalink=permalink,
            title=title,
            content=content,
            content_hash=content_hash,
            source_app=source_app,
            modified_at=modified_at,
            **kwargs,
        )


# ── Base class ──────────────────────────────────────────────────────────────────

class BaseConnector(ABC):
    """
    Abstract base class all Cortex connectors must implement.

    Subclasses set class-level attributes:
        app_id       = "slack"
        display_name = "Slack"
        auth_type    = "oauth2"  # or "api_key" | "none"
    """

    app_id: str = ""
    display_name: str = ""
    auth_type: Literal["none", "api_key", "oauth2"] = "api_key"

    def __init__(self):
        self._last_sync_cursor: Optional[str] = None

    # ── Required interface ─────────────────────────────────────────────────────

    @abstractmethod
    def connect(self, credentials: Dict[str, str]) -> ConnectResult:
        """
        Validate credentials and establish connection.
        For OAuth connectors: exchange token, store in keyring.
        For API key connectors: test the key against the API.
        Returns ConnectResult with success/failure.
        """

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Quick connectivity check using stored credentials.
        Does NOT modify stored state.
        Returns True if the connection is healthy.
        """

    @abstractmethod
    def list_resources(self) -> List[Resource]:
        """
        List selectable resources available to sync.
        Called after connect() to let the user pick channels/pages/folders.
        Examples: Slack channels, Notion pages, Drive folders.
        Returns empty list if connector doesn't have selectable resources.
        """

    @abstractmethod
    def fetch_delta(self, since: Optional[str] = None) -> List[RawDocument]:
        """
        Fetch only content that changed since `since` cursor.
        `since` is an opaque string (ISO timestamp or pagination token).
        If `since` is None → perform a full initial fetch.
        Returns list of RawDocument objects ready for the pipeline.
        """

    @abstractmethod
    def get_permalink(self, location_key: str) -> str:
        """
        Convert a location_key back into a human-clickable URL.
        Used when reconstructing PointerRecord permalinks.
        """

    # ── Cursor management (default implementation) ─────────────────────────────

    @property
    def last_sync_cursor(self) -> Optional[str]:
        """
        Opaque cursor marking the last successfully synced position.
        Persisted externally by SyncManager; restored here before fetch_delta().
        """
        return self._last_sync_cursor

    @last_sync_cursor.setter
    def last_sync_cursor(self, cursor: str) -> None:
        self._last_sync_cursor = cursor

    # ── Convenience helpers ────────────────────────────────────────────────────

    @staticmethod
    def now_iso() -> str:
        """Return current UTC time as ISO 8601 string (used as default cursor)."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def hash_content(content: str) -> str:
        """SHA256 of content, first 16 hex chars. Used for change detection."""
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} app_id={self.app_id!r}>"
