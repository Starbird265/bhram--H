"""
Phase 2 — Pointer-Based Memory

PointerRecord: stores ONLY the address + hash of each document.
NO raw content is ever written to the pointer table.

Why this matters for token efficiency:
  - Before any AI call, the pipeline checks pointer_records
  - If SHA256(content)[:16] matches last_seen_hash → content unchanged
  - The entire AI distillation step is SKIPPED → 0 tokens spent
  - Only NEW or CHANGED documents are ever sent to the LLM

This is the primary mechanism for the "memory should only store
location addresses" requirement. Documents are referenced by:
  location_key: stable identifier for the source object
               ("channel_id/ts", "notion_page_id", "gdrive_file_id", etc.)
  content_hash: first 16 chars of SHA256(content)
               changes when and only when content changes

The PointerStore is integrated into SQLiteStore via schema migration.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Set


# ── Data model ──────────────────────────────────────────────────────────────────

@dataclass
class PointerRecord:
    """
    Lightweight address record for a document in the pointer store.
    Stores location + hash + permalink. Never stores raw content.
    """
    location_key: str          # Stable source-side ID (e.g. "slack:C123/1716200000.0")
    app_id: str                # Which connector owns this ("slack", "notion", …)
    content_hash: str          # SHA256(content)[:16] — change detector
    permalink: str             # Direct URL to the source document
    title: str                 # Human-readable title for UI display
    last_seen_at: str          # ISO timestamp of last successful fetch
    last_indexed_at: Optional[str] = None  # ISO timestamp of last AI processing
    department: str = "shared"
    byte_size: int = 0         # Approximate content size (for rate budgeting)

    def is_stale(self, new_hash: str) -> bool:
        """Return True if content has changed since last index."""
        return self.content_hash != new_hash

    @staticmethod
    def hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class SyncState:
    """
    Per-connector sync cursor. Persisted between runs.
    Loaded at start of each connector run, saved at end.
    """
    app_id: str
    cursor: Optional[str] = None   # Last sync position (ISO ts, Slack ts, delta token)
    last_run_at: Optional[str] = None
    total_docs_seen: int = 0
    total_docs_skipped: int = 0     # Hash-gate skips (=token savings)
    total_docs_processed: int = 0   # Actually sent to AI


# ── PointerStore ────────────────────────────────────────────────────────────────

class PointerStore:
    """
    SQLite-backed address book for all ingested documents.

    The PointerStore is the ONLY place Cortex stores document metadata.
    Raw content never lives here — only location + hash + permalink.

    Two tables:
      pointer_records: one row per document, keyed by location_key
      sync_state:      one row per connector, stores the delta cursor

    Schema is auto-migrated into the existing bhrm.db on first use.
    """

    def __init__(self, db_path: str):
        self.db_file = Path(db_path) / "bhrm.db"
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_file), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self):
        """
        Add pointer_records and sync_state tables to the existing DB.
        Safe to call multiple times (all CREATE IF NOT EXISTS).
        """
        with self._connect() as conn:
            conn.executescript("""
                -- ═══ POINTER RECORDS — the address book ═══
                CREATE TABLE IF NOT EXISTS pointer_records (
                    location_key     TEXT PRIMARY KEY,
                    app_id           TEXT NOT NULL,
                    content_hash     TEXT NOT NULL,
                    permalink        TEXT NOT NULL DEFAULT '',
                    title            TEXT NOT NULL DEFAULT '',
                    last_seen_at     TEXT NOT NULL,
                    last_indexed_at  TEXT,
                    department       TEXT NOT NULL DEFAULT 'shared',
                    byte_size        INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_ptr_app_id
                    ON pointer_records(app_id);
                CREATE INDEX IF NOT EXISTS idx_ptr_last_seen
                    ON pointer_records(last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_ptr_last_indexed
                    ON pointer_records(last_indexed_at);

                -- ═══ SYNC STATE — per-connector delta cursor ═══
                CREATE TABLE IF NOT EXISTS sync_state (
                    app_id                TEXT PRIMARY KEY,
                    cursor                TEXT,
                    last_run_at           TEXT,
                    total_docs_seen       INTEGER NOT NULL DEFAULT 0,
                    total_docs_skipped    INTEGER NOT NULL DEFAULT 0,
                    total_docs_processed  INTEGER NOT NULL DEFAULT 0
                );
            """)

    # ── Pointer record operations ────────────────────────────────────────────────

    def upsert(self, record: PointerRecord) -> None:
        """Insert or update a pointer record. Never stores content."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO pointer_records (
                    location_key, app_id, content_hash, permalink, title,
                    last_seen_at, last_indexed_at, department, byte_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(location_key) DO UPDATE SET
                    content_hash    = excluded.content_hash,
                    permalink       = excluded.permalink,
                    title           = excluded.title,
                    last_seen_at    = excluded.last_seen_at,
                    -- only update last_indexed_at if new value is not NULL
                    last_indexed_at = COALESCE(excluded.last_indexed_at, last_indexed_at),
                    department      = excluded.department,
                    byte_size       = excluded.byte_size
            """, (
                record.location_key, record.app_id, record.content_hash,
                record.permalink, record.title, record.last_seen_at,
                record.last_indexed_at, record.department, record.byte_size,
            ))

    def mark_indexed(self, location_key: str) -> None:
        """Mark a document as processed by the AI pipeline."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE pointer_records SET last_indexed_at = ? WHERE location_key = ?",
                (now, location_key)
            )

    def get(self, location_key: str) -> Optional[PointerRecord]:
        """Retrieve a pointer record by location key."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pointer_records WHERE location_key = ?",
                (location_key,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_stale_keys(self, app_id: str, current_hashes: Dict[str, str]) -> Set[str]:
        """
        Given a dict of {location_key: current_hash}, return the set of
        location_keys that need AI processing (new OR hash-changed).

        This is the core hash-gate. The returned set is the only subset
        of documents that will be sent to the LLM — everything else is skipped.
        """
        stale = set()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT location_key, content_hash FROM pointer_records WHERE app_id = ?",
                (app_id,)
            ).fetchall()
        known = {r["location_key"]: r["content_hash"] for r in rows}

        for loc_key, new_hash in current_hashes.items():
            existing_hash = known.get(loc_key)
            if existing_hash is None or existing_hash != new_hash:
                stale.add(loc_key)

        return stale

    def delete(self, location_key: str) -> None:
        """Remove a pointer when a document is deleted at source."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM pointer_records WHERE location_key = ?",
                (location_key,)
            )

    def count(self, app_id: Optional[str] = None) -> int:
        with self._connect() as conn:
            if app_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM pointer_records WHERE app_id = ?", (app_id,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM pointer_records").fetchone()
        return row[0]

    def list_by_app(self, app_id: str, limit: int = 500) -> List[PointerRecord]:
        """List all pointer records for an app."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pointer_records WHERE app_id = ? ORDER BY last_seen_at DESC LIMIT ?",
                (app_id, limit)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_stats(self) -> Dict[str, Any]:
        """Dashboard summary — total docs, breakdown by app, unindexed count."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM pointer_records").fetchone()[0]
            by_app = conn.execute("""
                SELECT app_id, COUNT(*) as cnt,
                       SUM(CASE WHEN last_indexed_at IS NULL THEN 1 ELSE 0 END) as unindexed,
                       SUM(byte_size) as total_bytes
                FROM pointer_records
                GROUP BY app_id
            """).fetchall()
        return {
            "total_pointers": total,
            "by_app": [
                {
                    "app_id": r["app_id"],
                    "count": r["cnt"],
                    "unindexed": r["unindexed"],
                    "total_bytes": r["total_bytes"],
                }
                for r in by_app
            ],
        }

    # ── Sync state operations ───────────────────────────────────────────────────

    def load_sync_state(self, app_id: str) -> SyncState:
        """Load the sync cursor for a connector. Returns empty state if first run."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_state WHERE app_id = ?", (app_id,)
            ).fetchone()
        if not row:
            return SyncState(app_id=app_id)
        return SyncState(
            app_id=row["app_id"],
            cursor=row["cursor"],
            last_run_at=row["last_run_at"],
            total_docs_seen=row["total_docs_seen"],
            total_docs_skipped=row["total_docs_skipped"],
            total_docs_processed=row["total_docs_processed"],
        )

    def save_sync_state(self, state: SyncState) -> None:
        """Persist the sync cursor after a successful connector run."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO sync_state (
                    app_id, cursor, last_run_at,
                    total_docs_seen, total_docs_skipped, total_docs_processed
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    cursor               = excluded.cursor,
                    last_run_at          = excluded.last_run_at,
                    total_docs_seen      = excluded.total_docs_seen,
                    total_docs_skipped   = excluded.total_docs_skipped,
                    total_docs_processed = excluded.total_docs_processed
            """, (
                state.app_id, state.cursor, state.last_run_at,
                state.total_docs_seen, state.total_docs_skipped,
                state.total_docs_processed,
            ))

    def get_all_sync_states(self) -> List[SyncState]:
        """Get sync state for all connectors."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sync_state").fetchall()
        return [SyncState(
            app_id=r["app_id"],
            cursor=r["cursor"],
            last_run_at=r["last_run_at"],
            total_docs_seen=r["total_docs_seen"],
            total_docs_skipped=r["total_docs_skipped"],
            total_docs_processed=r["total_docs_processed"],
        ) for r in rows]

    # ── Internal ────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> PointerRecord:
        return PointerRecord(
            location_key=row["location_key"],
            app_id=row["app_id"],
            content_hash=row["content_hash"],
            permalink=row["permalink"],
            title=row["title"],
            last_seen_at=row["last_seen_at"],
            last_indexed_at=row["last_indexed_at"],
            department=row["department"],
            byte_size=row["byte_size"],
        )
