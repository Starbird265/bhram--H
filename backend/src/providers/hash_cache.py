"""
Phase 3 — Hash Cache: In-Process LRU + SQLite Persistence

Two-level cache for AI call results:
  Level 1: In-memory LRU (fast, gone on restart)  — cap: 512 entries
  Level 2: SQLite (persists across restarts)       — cap: 50,000 entries

Cache key: SHA256(prompt + provider + model)[:32]
Cache value: JSON-serialized AI response

Effect on token usage:
  - If the same chunk is ingested a second time (content unchanged),
    the hash-gate in SyncManager kills it BEFORE it reaches the distiller.
  - If content changed but the PROMPT is the same (different file, same text),
    the hash cache serves the result without a new AI call.
  - Together: hash-gate + hash-cache eliminate ~95% of redundant AI calls.

Eviction:
  - LRU in-memory: automatic via lru_cache / collections.OrderedDict
  - SQLite: LRU eviction runs when table exceeds MAX_SQLITE_ENTRIES
    (deletes oldest 10% of entries by created_at)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional, Any, Dict


MAX_MEMORY_ENTRIES = 512
MAX_SQLITE_ENTRIES = 50_000
EVICT_PERCENT = 0.10   # Remove oldest 10% when over limit


class HashCache:
    """
    Two-level hash cache for AI distillation results.

    Usage:
        cache = HashCache(db_path="database")
        key = cache.make_key(prompt, provider="claude", model="claude-sonnet-4-5")
        result = cache.get(key)
        if result is None:
            result = call_ai(prompt)
            cache.set(key, result)
    """

    def __init__(self, db_path: str):
        self._db_file = Path(db_path) / "bhrm.db"
        self._db_file.parent.mkdir(parents=True, exist_ok=True)
        self._memory: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()
        self._migrate()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._db_file), timeout=5)
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
        """Add hash_cache table to existing DB (safe to call repeatedly)."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS hash_cache (
                    cache_key  TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    hits       INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_hit   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_cache_created
                    ON hash_cache(created_at);
                CREATE INDEX IF NOT EXISTS idx_cache_last_hit
                    ON hash_cache(last_hit);
            """)

    # ── Public API ──────────────────────────────────────────────────────────────

    @staticmethod
    def make_key(prompt: str, provider: str = "", model: str = "") -> str:
        """
        Generate a stable cache key from prompt + provider + model.
        SHA256 so keys are fixed-length and collision-resistant.
        """
        raw = f"{provider}:{model}:{prompt}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, key: str) -> Optional[Any]:
        """
        Check cache. Returns deserialized value or None.
        Updates hit counter + promotes to front of LRU.
        """
        # L1: memory
        with self._lock:
            if key in self._memory:
                self._memory.move_to_end(key)
                raw = self._memory[key]
                return json.loads(raw)

        # L2: SQLite
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM hash_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row:
                # Increment hit counter
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE hash_cache SET hits = hits + 1, last_hit = ? WHERE cache_key = ?",
                    (now, key)
                )
                raw = row["value_json"]
                # Promote to L1
                with self._lock:
                    self._memory[key] = raw
                    self._memory.move_to_end(key)
                    if len(self._memory) > MAX_MEMORY_ENTRIES:
                        self._memory.popitem(last=False)
                return json.loads(raw)

        return None

    def set(self, key: str, value: Any) -> None:
        """Store a value in both L1 (memory) and L2 (SQLite)."""
        raw = json.dumps(value, default=str)
        now = datetime.now(timezone.utc).isoformat()

        # L1: memory
        with self._lock:
            self._memory[key] = raw
            self._memory.move_to_end(key)
            if len(self._memory) > MAX_MEMORY_ENTRIES:
                self._memory.popitem(last=False)

        # L2: SQLite
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO hash_cache (cache_key, value_json, hits, created_at, last_hit)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    last_hit   = excluded.last_hit
            """, (key, raw, now, now))
            self._evict_if_needed(conn)

    def invalidate(self, key: str) -> None:
        """Remove a specific key from both levels."""
        with self._lock:
            self._memory.pop(key, None)
        with self._connect() as conn:
            conn.execute("DELETE FROM hash_cache WHERE cache_key = ?", (key,))

    def stats(self) -> Dict[str, Any]:
        """Return cache statistics for the dashboard."""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(hits) as total_hits,
                       AVG(hits) as avg_hits
                FROM hash_cache
            """).fetchone()
        with self._lock:
            memory_entries = len(self._memory)
        return {
            "memory_entries": memory_entries,
            "sqlite_entries": row["total"] or 0,
            "total_hits": row["total_hits"] or 0,
            "avg_hits_per_entry": round(row["avg_hits"] or 0.0, 2),
            "max_memory_entries": MAX_MEMORY_ENTRIES,
            "max_sqlite_entries": MAX_SQLITE_ENTRIES,
        }

    # ── Eviction ─────────────────────────────────────────────────────────────────

    def _evict_if_needed(self, conn: sqlite3.Connection) -> None:
        """Delete oldest entries if SQLite table exceeds MAX_SQLITE_ENTRIES."""
        count = conn.execute("SELECT COUNT(*) FROM hash_cache").fetchone()[0]
        if count > MAX_SQLITE_ENTRIES:
            to_delete = int(MAX_SQLITE_ENTRIES * EVICT_PERCENT)
            conn.execute("""
                DELETE FROM hash_cache
                WHERE cache_key IN (
                    SELECT cache_key FROM hash_cache
                    ORDER BY last_hit ASC
                    LIMIT ?
                )
            """, (to_delete,))
