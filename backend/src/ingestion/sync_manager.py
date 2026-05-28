"""
Phase 3 — SyncManager: Delta Sync Orchestrator

Ties together:
  1. Connector.fetch_delta(since=cursor) — only fetches changed docs
  2. PointerStore.get_stale_keys()       — hash-gates to skip unchanged
  3. ModelRouter.route()                 — picks cheapest model per doc
  4. HashCache.get/set()                 — in-memory + SQLite LRU cache

Token economics: for a 1000-doc workspace:
  - Without hash-gate: 1000 AI calls per run
  - With hash-gate (5% change rate): 50 AI calls per run
  - Net saving: ~95% token reduction on steady-state runs

Usage:
    manager = SyncManager(db_path="database")
    results = await manager.run("slack", connector=slack_connector)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, TYPE_CHECKING

from storage.pointer_store import PointerStore, PointerRecord, SyncState

if TYPE_CHECKING:
    from ingestion.connectors.base import BaseConnector, RawDocument


@dataclass
class SyncResult:
    app_id: str
    docs_fetched: int = 0
    docs_skipped: int = 0          # Hash-gate wins (unchanged)
    docs_processed: int = 0        # Actually sent to AI
    docs_failed: int = 0
    tokens_saved_estimate: int = 0  # Rough token estimate saved by hash-gate
    duration_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)


class SyncManager:
    """
    Orchestrates delta sync for all connectors.

    For each run:
      1. Load sync cursor from PointerStore
      2. Call connector.fetch_delta(since=cursor) → new/changed docs only
      3. Compute SHA256 hashes for all fetched docs
      4. Call pointer_store.get_stale_keys() → only truly changed subset
      5. Send stale docs to pipeline (AI distillation)
      6. Upsert pointer records for all fetched docs
      7. Update cursor + stats in PointerStore
    """

    def __init__(self, db_path: str):
        self.pointer_store = PointerStore(db_path=db_path)

    def run_sync(
        self,
        app_id: str,
        connector: "BaseConnector",
        pipeline_fn: Optional[Any] = None,
    ) -> SyncResult:
        """
        Synchronous entry point. Runs a full delta sync for one connector.

        pipeline_fn: optional callable(docs: List[RawDocument]) that sends
                     the stale docs through the AI pipeline. If None, docs
                     are returned in SyncResult for the caller to process.
        """
        result = SyncResult(app_id=app_id)
        t0 = time.time()

        # 1. Load cursor
        state = self.pointer_store.load_sync_state(app_id)
        cursor = state.cursor
        print(f"  [SyncManager/{app_id}] Starting. Last cursor: {cursor or 'none (full sync)'}")

        # 2. Fetch delta from connector
        try:
            raw_docs: List["RawDocument"] = connector.fetch_delta(since=cursor)
        except Exception as e:
            result.errors.append(f"fetch_delta failed: {e}")
            result.duration_seconds = time.time() - t0
            return result

        result.docs_fetched = len(raw_docs)
        if not raw_docs:
            print(f"  [SyncManager/{app_id}] No new documents since {cursor}. Done.")
            result.duration_seconds = time.time() - t0
            return result

        # 3. Build hash map for all fetched docs
        hash_map: Dict[str, str] = {
            doc.location_key: doc.content_hash for doc in raw_docs
        }

        # 4. Hash-gate: find stale keys (new OR changed content)
        stale_keys = self.pointer_store.get_stale_keys(app_id, hash_map)
        stale_docs = [d for d in raw_docs if d.location_key in stale_keys]
        skipped_docs = [d for d in raw_docs if d.location_key not in stale_keys]

        result.docs_skipped = len(skipped_docs)
        result.docs_processed = len(stale_docs)
        # Rough estimate: avg 500 tokens per doc for distillation
        result.tokens_saved_estimate = result.docs_skipped * 500

        print(f"  [SyncManager/{app_id}] {result.docs_fetched} fetched, "
              f"{result.docs_skipped} skipped (unchanged), "
              f"{result.docs_processed} to process. "
              f"~{result.tokens_saved_estimate:,} tokens saved.")

        # 5. Send stale docs to pipeline
        if stale_docs and pipeline_fn is not None:
            try:
                pipeline_fn(stale_docs)
            except Exception as e:
                result.errors.append(f"pipeline failed: {e}")
                result.docs_failed = result.docs_processed
                result.docs_processed = 0

        # 6. Upsert pointer records for ALL fetched docs (seen_at = now)
        now = datetime.now(timezone.utc).isoformat()
        for doc in raw_docs:
            record = PointerRecord(
                location_key=doc.location_key,
                app_id=app_id,
                content_hash=doc.content_hash,
                permalink=doc.permalink,
                title=doc.title,
                last_seen_at=now,
                last_indexed_at=now if doc.location_key in stale_keys else None,
                department=getattr(doc, "department", "shared"),
                byte_size=len(doc.content.encode()),
            )
            try:
                self.pointer_store.upsert(record)
            except Exception as e:
                result.errors.append(f"pointer upsert failed for {doc.location_key}: {e}")

        # 7. Save cursor + stats
        new_cursor = connector.last_sync_cursor or now
        new_state = SyncState(
            app_id=app_id,
            cursor=new_cursor,
            last_run_at=now,
            total_docs_seen=state.total_docs_seen + result.docs_fetched,
            total_docs_skipped=state.total_docs_skipped + result.docs_skipped,
            total_docs_processed=state.total_docs_processed + result.docs_processed,
        )
        self.pointer_store.save_sync_state(new_state)

        result.duration_seconds = time.time() - t0
        print(f"  [SyncManager/{app_id}] Done in {result.duration_seconds:.1f}s.")
        return result

    def get_dashboard(self) -> Dict[str, Any]:
        """Return token-saving stats for the /api/memory/pointer-stats endpoint."""
        states = self.pointer_store.get_all_sync_states()
        pointer_stats = self.pointer_store.get_stats()

        total_seen = sum(s.total_docs_seen for s in states)
        total_skipped = sum(s.total_docs_skipped for s in states)
        skip_rate = (total_skipped / total_seen * 100) if total_seen > 0 else 0.0
        tokens_saved = total_skipped * 500  # ~500 tokens per skip

        return {
            "pointer_stats": pointer_stats,
            "sync_stats": {
                "total_docs_seen": total_seen,
                "total_docs_skipped": total_skipped,
                "total_docs_processed": total_seen - total_skipped,
                "skip_rate_percent": round(skip_rate, 1),
                "estimated_tokens_saved": tokens_saved,
                "estimated_cost_saved_usd": round(tokens_saved / 1_000_000 * 3.0, 4),
            },
            "connectors": [
                {
                    "app_id": s.app_id,
                    "last_run": s.last_run_at,
                    "cursor": s.cursor,
                    "docs_seen": s.total_docs_seen,
                    "docs_skipped": s.total_docs_skipped,
                    "docs_processed": s.total_docs_processed,
                }
                for s in states
            ],
        }
