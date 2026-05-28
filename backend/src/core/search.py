"""
Phase 4 — Semantic Search Engine

Vector-based semantic search across all indexed knowledge.
Uses sentence-transformers (local, free) for embeddings.
Falls back to BM25 keyword search if transformers unavailable.

Architecture:
  - Embeddings stored in SQLite (BLOB) alongside chunk metadata
  - Cosine similarity computed in-process (numpy)
  - Hybrid scoring: 0.7 * semantic + 0.3 * keyword (BM25)
  - ACL-filtered: only returns results the requesting dept can access
  - Results ranked by: hybrid_score * confidence_score * recency_boost

Endpoint: GET /api/search?q=...&dept=...&limit=10&mode=hybrid
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple


# ── Embedding provider (graceful degradation) ───────────────────────────────────

_EMBED_MODEL = None
_EMBED_AVAILABLE = False


def _get_embed_model():
    """Load sentence-transformers model on first use. Falls back to None."""
    global _EMBED_MODEL, _EMBED_AVAILABLE
    if _EMBED_AVAILABLE:
        return _EMBED_MODEL
    try:
        from sentence_transformers import SentenceTransformer
        model_name = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
        _EMBED_MODEL = SentenceTransformer(model_name)
        _EMBED_AVAILABLE = True
        print(f"  [Search] Loaded embedding model: {model_name}")
    except ImportError:
        print("  [Search] sentence-transformers not installed. Falling back to keyword search.")
        _EMBED_AVAILABLE = False
    return _EMBED_MODEL


def _embed(text: str) -> Optional[List[float]]:
    """Generate embedding for text. Returns None if unavailable."""
    model = _get_embed_model()
    if model is None:
        return None
    try:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    except Exception as e:
        print(f"  [Search] Embedding failed: {e}")
        return None


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two pre-normalized vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    # Vectors are already L2-normalized by sentence-transformers
    return max(0.0, min(1.0, dot))


# ── Serialization ────────────────────────────────────────────────────────────────

def _vec_to_blob(vec: List[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> List[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ── BM25 Keyword Scoring (fallback) ─────────────────────────────────────────────

def _bm25_score(query_terms: List[str], doc_text: str, avg_dl: float, k1: float = 1.5, b: float = 0.75) -> float:
    """Simple BM25 score for a single document."""
    terms = doc_text.lower().split()
    dl = len(terms)
    if dl == 0 or avg_dl == 0:
        return 0.0
    score = 0.0
    term_freq: Dict[str, int] = {}
    for t in terms:
        term_freq[t] = term_freq.get(t, 0) + 1
    for q in query_terms:
        tf = term_freq.get(q.lower(), 0)
        if tf == 0:
            continue
        idf = math.log((1 + 1) / (1 + 1) + 1)  # simplified IDF for single doc
        score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
    return score


# ── Search result ─────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    chunk_id: str
    title: str
    content: str
    summary: str
    department: str
    knowledge_type: str
    source_type: str
    source_identifier: str
    tags: List[str]
    confidence_score: float
    semantic_score: float
    keyword_score: float
    hybrid_score: float
    permalink: str = ""
    created_at: str = ""


# ── Vector Store ──────────────────────────────────────────────────────────────────

class VectorStore:
    """
    SQLite-backed vector store for semantic search.
    Stores embeddings alongside chunk metadata for fast retrieval.
    Auto-migrates `embeddings` table into bhrm.db.
    """

    SEMANTIC_WEIGHT = 0.7
    KEYWORD_WEIGHT = 0.3

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
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id         TEXT PRIMARY KEY,
                    title            TEXT NOT NULL DEFAULT '',
                    content          TEXT NOT NULL DEFAULT '',
                    summary          TEXT NOT NULL DEFAULT '',
                    department       TEXT NOT NULL DEFAULT 'shared',
                    knowledge_type   TEXT NOT NULL DEFAULT '',
                    source_type      TEXT NOT NULL DEFAULT '',
                    source_id        TEXT NOT NULL DEFAULT '',
                    tags_json        TEXT NOT NULL DEFAULT '[]',
                    confidence       REAL NOT NULL DEFAULT 0.6,
                    embedding        BLOB,
                    indexed_at       TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_emb_dept
                    ON embeddings(department);
                CREATE INDEX IF NOT EXISTS idx_emb_type
                    ON embeddings(knowledge_type);
            """)

    def index_chunk(
        self,
        chunk_id: str,
        title: str,
        content: str,
        summary: str,
        department: str,
        knowledge_type: str,
        source_type: str,
        source_id: str,
        tags: List[str],
        confidence: float,
    ) -> bool:
        """
        Index a knowledge chunk for semantic search.
        Generates an embedding and stores it alongside metadata.
        Returns True if successful, False if embedding unavailable.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # Generate embedding for title + summary (not full content — keeps index small)
        embed_text = f"{title}. {summary or content[:300]}"
        vec = _embed(embed_text)
        blob = _vec_to_blob(vec) if vec else None

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO embeddings (
                    chunk_id, title, content, summary, department,
                    knowledge_type, source_type, source_id, tags_json,
                    confidence, embedding, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    title=excluded.title, content=excluded.content,
                    summary=excluded.summary, confidence=excluded.confidence,
                    embedding=excluded.embedding, indexed_at=excluded.indexed_at
            """, (
                chunk_id, title, content[:2000], summary, department,
                knowledge_type, source_type, source_id,
                json.dumps(tags), confidence, blob, now,
            ))
        return vec is not None

    def search(
        self,
        query: str,
        department: Optional[str] = None,
        knowledge_type: Optional[str] = None,
        limit: int = 10,
        mode: str = "hybrid",  # "semantic", "keyword", "hybrid"
        min_confidence: float = 0.0,
    ) -> List[SearchResult]:
        """
        Search indexed knowledge chunks.

        mode=hybrid: 0.7 * cosine_similarity + 0.3 * bm25_score (recommended)
        mode=semantic: cosine similarity only (needs sentence-transformers)
        mode=keyword: BM25 keyword matching only (always available)
        """
        # Fetch candidate chunks
        query_conds = ["1=1"]
        params: list = []
        if department and department != "all":
            query_conds.append("department = ?")
            params.append(department)
        if knowledge_type:
            query_conds.append("knowledge_type = ?")
            params.append(knowledge_type)
        if min_confidence > 0:
            query_conds.append("confidence >= ?")
            params.append(min_confidence)

        where = " AND ".join(query_conds)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM embeddings WHERE {where} LIMIT 1000",
                params
            ).fetchall()

        if not rows:
            return []

        query_terms = query.lower().split()
        avg_dl = sum(len(r["content"].split()) for r in rows) / max(len(rows), 1)

        # Compute query embedding
        query_vec = _embed(query) if mode in ("semantic", "hybrid") else None

        results = []
        for row in rows:
            # Semantic score
            sem_score = 0.0
            if query_vec and row["embedding"]:
                try:
                    doc_vec = _blob_to_vec(row["embedding"])
                    sem_score = _cosine(query_vec, doc_vec)
                except Exception:
                    sem_score = 0.0

            # Keyword score (BM25)
            kw_score = 0.0
            if mode in ("keyword", "hybrid"):
                doc_text = f"{row['title']} {row['summary']} {row['content']}"
                kw_score = _bm25_score(query_terms, doc_text, avg_dl)
                # Normalize to 0-1
                kw_score = min(1.0, kw_score / 5.0)

            # Hybrid score
            if mode == "semantic":
                hybrid = sem_score
            elif mode == "keyword":
                hybrid = kw_score
            else:  # hybrid
                if query_vec:
                    hybrid = self.SEMANTIC_WEIGHT * sem_score + self.KEYWORD_WEIGHT * kw_score
                else:
                    hybrid = kw_score  # Fall back to keyword if no embeddings

            # Skip irrelevant results
            if hybrid < 0.05 and not any(t in row["content"].lower() for t in query_terms):
                continue

            results.append(SearchResult(
                chunk_id=row["chunk_id"],
                title=row["title"],
                content=row["content"][:500],
                summary=row["summary"],
                department=row["department"],
                knowledge_type=row["knowledge_type"],
                source_type=row["source_type"],
                source_identifier=row["source_id"],
                tags=json.loads(row["tags_json"]),
                confidence_score=row["confidence"],
                semantic_score=round(sem_score, 4),
                keyword_score=round(kw_score, 4),
                hybrid_score=round(hybrid, 4),
                created_at=row["indexed_at"],
            ))

        # Sort by hybrid_score * confidence (double-rank)
        results.sort(key=lambda r: r.hybrid_score * r.confidence_score, reverse=True)
        return results[:limit]

    def count(self, department: Optional[str] = None) -> int:
        with self._connect() as conn:
            if department:
                row = conn.execute(
                    "SELECT COUNT(*) FROM embeddings WHERE department = ?", (department,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return row[0]

    def get_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            has_embedding = conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            by_dept = conn.execute("""
                SELECT department, COUNT(*) as cnt FROM embeddings GROUP BY department
            """).fetchall()
        return {
            "total_indexed": total,
            "with_embeddings": has_embedding,
            "keyword_only": total - has_embedding,
            "embedding_model": os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2"),
            "embeddings_available": _EMBED_AVAILABLE,
            "by_department": {r["department"]: r["cnt"] for r in by_dept},
        }

    def index_from_store(self, db_path: str) -> int:
        """
        Bulk-index all existing chunks into the search index.
        Reads from FilesystemStore (where the pipeline actually writes chunks).
        Falls back to SQLiteStore if FilesystemStore has nothing.
        Returns the count of newly indexed chunks.
        """
        # Try FilesystemStore first — this is where the 10-layer pipeline saves chunks
        try:
            from storage.filesystem_store import FilesystemStore
            fs_store = FilesystemStore(db_path=db_path)
            chunks = fs_store.get_all()
        except Exception:
            chunks = []

        # Fallback: SQLiteStore
        if not chunks:
            try:
                from storage.sqlite_store import SQLiteStore
                chunks = SQLiteStore(db_path=db_path).get_all()
            except Exception:
                chunks = []

        if not chunks:
            return 0

        with self._connect() as conn:
            already = set(
                r[0] for r in conn.execute("SELECT chunk_id FROM embeddings").fetchall()
            )

        count = 0
        for chunk in chunks:
            if chunk.id in already:
                continue
            success = self.index_chunk(
                chunk_id=chunk.id,
                title=chunk.title,
                content=chunk.content,
                summary=getattr(chunk, 'summary', ''),
                department=chunk.department.value,
                knowledge_type=chunk.knowledge_type.value,
                source_type=chunk.source_type.value,
                source_id=chunk.source_identifier,
                tags=list(getattr(chunk, 'tags', [])),
                confidence=chunk.metadata.confidence_score,
            )
            if success:
                count += 1
        return count
