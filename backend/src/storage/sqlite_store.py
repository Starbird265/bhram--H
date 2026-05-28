"""
Storage Layer: SQLite Store
ACID-compliant, indexed storage for all memory tiers.

Replaces the JSON-based FilesystemStore with:
  - Indexed queries by department, type, confidence, timestamp
  - Atomic writes (no full-file rewrites)
  - Support for 8-tier memory architecture
  - Same public interface as FilesystemStore for backward compatibility
"""

import json
import sqlite3
import os
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from contextlib import contextmanager
from datetime import datetime, timezone

from core.models import (
    KnowledgeChunk, Department, KnowledgeType, SourceType,
    ProcessingLayer, KnowledgeMetadata, SourcePosition,
    AtomicKnowledgeUnit, SensitivityLevel, UnitStatus, MemoryTier,
    SourceRef, AuditEntry, PrivacyMode,
    SessionFact, UserProfile,
)


class SQLiteStore:
    """
    SQLite-backed storage engine for the 10-layer pipeline.

    Tables:
      - chunks:         KnowledgeChunk records (backward compat with FilesystemStore)
      - atomic_units:   AtomicKnowledgeUnit records (base field for skill generation)
      - source_refs:    Immutable source references (source memory)
      - audit_log:      Decision trail entries (audit memory)
      - failure_memory:  Correction and incident records
      - session_memory: Per-conversation facts from agent/user interactions (v2)
    """

    SCHEMA_VERSION = 2

    def __init__(self, db_path: str = "database"):
        self.db_dir = Path(db_path)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_file = self.db_dir / "bhrm.db"
        self._init_db()

    @contextmanager
    def _connect(self):
        """Context manager for database connections with WAL mode."""
        conn = sqlite3.connect(str(self.db_file), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Create all tables and indexes if they don't exist."""
        with self._connect() as conn:
            conn.executescript("""
                -- Schema version tracking
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                -- ═══ CHUNKS TABLE (backward compat with FilesystemStore) ═══
                CREATE TABLE IF NOT EXISTS chunks (
                    id                TEXT PRIMARY KEY,
                    department        TEXT NOT NULL,
                    knowledge_type    TEXT NOT NULL,
                    source_type       TEXT NOT NULL,
                    source_identifier TEXT NOT NULL,
                    title             TEXT NOT NULL,
                    content           TEXT NOT NULL,
                    summary           TEXT NOT NULL DEFAULT '',
                    tags              TEXT NOT NULL DEFAULT '[]',
                    processing_layer  TEXT NOT NULL DEFAULT 'raw',
                    metadata_json     TEXT NOT NULL DEFAULT '{}',
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_dept
                    ON chunks(department);
                CREATE INDEX IF NOT EXISTS idx_chunks_type
                    ON chunks(knowledge_type);
                CREATE INDEX IF NOT EXISTS idx_chunks_layer
                    ON chunks(processing_layer);

                -- ═══ ATOMIC UNITS TABLE ═══
                CREATE TABLE IF NOT EXISTS atomic_units (
                    id                  TEXT PRIMARY KEY,
                    claim               TEXT NOT NULL,
                    instruction         TEXT NOT NULL,
                    rationale           TEXT,
                    knowledge_type      TEXT NOT NULL,
                    department          TEXT NOT NULL,
                    scope               TEXT NOT NULL DEFAULT 'global',
                    applies_when        TEXT NOT NULL DEFAULT '[]',
                    does_not_apply_when TEXT NOT NULL DEFAULT '[]',
                    source_type         TEXT NOT NULL,
                    source_identifier   TEXT NOT NULL,
                    source_position_json TEXT,
                    source_excerpt_hash TEXT NOT NULL DEFAULT '',
                    source_reliability  REAL NOT NULL DEFAULT 1.0,
                    confidence_score    REAL NOT NULL DEFAULT 0.6,
                    verification_count  INTEGER NOT NULL DEFAULT 0,
                    last_confirmed_at   TEXT NOT NULL,
                    sensitivity_level   TEXT NOT NULL DEFAULT 'internal',
                    online_allowed      INTEGER NOT NULL DEFAULT 1,
                    tags                TEXT NOT NULL DEFAULT '[]',
                    entities            TEXT NOT NULL DEFAULT '[]',
                    tools_required      TEXT NOT NULL DEFAULT '[]',
                    related_units       TEXT NOT NULL DEFAULT '[]',
                    conflicts_with      TEXT NOT NULL DEFAULT '[]',
                    supersedes          TEXT NOT NULL DEFAULT '[]',
                    status              TEXT NOT NULL DEFAULT 'candidate',
                    memory_tier         TEXT NOT NULL DEFAULT 'working',
                    skill_targets       TEXT NOT NULL DEFAULT '[]',
                    validator_results   TEXT NOT NULL DEFAULT '{}',
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_units_dept
                    ON atomic_units(department);
                CREATE INDEX IF NOT EXISTS idx_units_status
                    ON atomic_units(status);
                CREATE INDEX IF NOT EXISTS idx_units_tier
                    ON atomic_units(memory_tier);
                CREATE INDEX IF NOT EXISTS idx_units_confidence
                    ON atomic_units(confidence_score);

                -- ═══ SOURCE REFS TABLE (immutable) ═══
                CREATE TABLE IF NOT EXISTS source_refs (
                    id                TEXT PRIMARY KEY,
                    source_type       TEXT NOT NULL,
                    source_identifier TEXT NOT NULL,
                    file_hash         TEXT,
                    acquired_at       TEXT NOT NULL,
                    byte_size         INTEGER,
                    privacy_mode      TEXT NOT NULL DEFAULT 'hard_local',
                    metadata_json     TEXT NOT NULL DEFAULT '{}'
                );

                -- ═══ AUDIT LOG TABLE (append-only) ═══
                CREATE TABLE IF NOT EXISTS audit_log (
                    id            TEXT PRIMARY KEY,
                    timestamp     TEXT NOT NULL,
                    action        TEXT NOT NULL,
                    actor         TEXT NOT NULL DEFAULT 'system',
                    target_type   TEXT NOT NULL,
                    target_id     TEXT NOT NULL,
                    details_json  TEXT NOT NULL DEFAULT '{}',
                    provider_used TEXT,
                    cost_usd      REAL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_target
                    ON audit_log(target_type, target_id);
                CREATE INDEX IF NOT EXISTS idx_audit_time
                    ON audit_log(timestamp);

                -- ═══ FAILURE MEMORY TABLE ═══
                CREATE TABLE IF NOT EXISTS failure_memory (
                    id                TEXT PRIMARY KEY,
                    related_unit_id   TEXT,
                    related_skill     TEXT,
                    issue_description TEXT NOT NULL,
                    correction        TEXT NOT NULL,
                    severity          TEXT NOT NULL DEFAULT 'medium',
                    reported_at       TEXT NOT NULL,
                    resolved          INTEGER NOT NULL DEFAULT 0,
                    metadata_json     TEXT NOT NULL DEFAULT '{}'
                );

                -- ═══ SESSION MEMORY TABLE (v2) ═══
                CREATE TABLE IF NOT EXISTS session_memory (
                    id                  TEXT PRIMARY KEY,
                    agent_id            TEXT,
                    user_id             TEXT,
                    fact                TEXT NOT NULL,
                    fact_type           TEXT NOT NULL DEFAULT 'explicit',
                    confidence          REAL NOT NULL DEFAULT 0.8,
                    source_conversation TEXT,
                    created_at          TEXT NOT NULL,
                    expires_at          TEXT,
                    promoted_to_unit_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_session_agent
                    ON session_memory(agent_id);
                CREATE INDEX IF NOT EXISTS idx_session_user
                    ON session_memory(user_id);
                CREATE INDEX IF NOT EXISTS idx_session_expires
                    ON session_memory(expires_at);
            """)

            # ── Schema migration: v1 → v2 ──
            # Add expires_at column to atomic_units if not present
            try:
                conn.execute("SELECT expires_at FROM atomic_units LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE atomic_units ADD COLUMN expires_at TEXT")

            # Track schema version
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("version", str(self.SCHEMA_VERSION))
            )

    # ═══════════════════════════════════════════════════════════════
    # CHUNK OPERATIONS (backward compat with FilesystemStore)
    # ═══════════════════════════════════════════════════════════════

    def save_chunk(self, chunk: KnowledgeChunk):
        """Save or update a KnowledgeChunk."""
        now = datetime.now(timezone.utc).isoformat()
        meta_json = chunk.metadata.model_dump_json()
        tags_json = json.dumps(chunk.tags)

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO chunks (
                    id, department, knowledge_type, source_type, source_identifier,
                    title, content, summary, tags, processing_layer,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    department=excluded.department,
                    knowledge_type=excluded.knowledge_type,
                    title=excluded.title,
                    content=excluded.content,
                    summary=excluded.summary,
                    tags=excluded.tags,
                    processing_layer=excluded.processing_layer,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
            """, (
                chunk.id, chunk.department.value, chunk.knowledge_type.value,
                chunk.source_type.value, chunk.source_identifier,
                chunk.title, chunk.content, chunk.summary,
                tags_json, chunk.processing_layer.value,
                meta_json, chunk.created_at.isoformat(), now,
            ))

    def get_all(self) -> List[KnowledgeChunk]:
        """Get all chunks."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM chunks").fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_all_by_department(self, department: Department) -> List[KnowledgeChunk]:
        """Get all chunks for a specific department."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE department = ?",
                (department.value,)
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_chunk_by_id(self, chunk_id: str) -> Optional[KnowledgeChunk]:
        """Get a single chunk by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
        return self._row_to_chunk(row) if row else None

    def delete_chunk(self, chunk_id: str):
        """Delete a chunk by ID."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))

    def count_chunks(self, department: Optional[Department] = None) -> int:
        """Count chunks, optionally filtered by department."""
        with self._connect() as conn:
            if department:
                row = conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE department = ?",
                    (department.value,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return row[0]

    def _row_to_chunk(self, row: sqlite3.Row) -> KnowledgeChunk:
        """Convert a SQLite row back to a KnowledgeChunk."""
        meta_data = json.loads(row["metadata_json"])
        metadata = KnowledgeMetadata(**meta_data)
        tags = json.loads(row["tags"])

        return KnowledgeChunk(
            id=row["id"],
            department=Department(row["department"]),
            knowledge_type=KnowledgeType(row["knowledge_type"]),
            source_type=SourceType(row["source_type"]),
            source_identifier=row["source_identifier"],
            title=row["title"],
            content=row["content"],
            summary=row["summary"],
            tags=tags,
            metadata=metadata,
            processing_layer=ProcessingLayer(row["processing_layer"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ═══════════════════════════════════════════════════════════════
    # ATOMIC UNIT OPERATIONS
    # ═══════════════════════════════════════════════════════════════

    def save_unit(self, unit: AtomicKnowledgeUnit):
        """Save or update an AtomicKnowledgeUnit."""
        now = datetime.now(timezone.utc).isoformat()
        pos_json = unit.source_position.model_dump_json() if unit.source_position else None

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO atomic_units (
                    id, claim, instruction, rationale,
                    knowledge_type, department, scope,
                    applies_when, does_not_apply_when,
                    source_type, source_identifier, source_position_json,
                    source_excerpt_hash, source_reliability,
                    confidence_score, verification_count, last_confirmed_at,
                    sensitivity_level, online_allowed,
                    tags, entities, tools_required,
                    related_units, conflicts_with, supersedes,
                    status, memory_tier, skill_targets, validator_results,
                    created_at, updated_at, expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    claim=excluded.claim,
                    instruction=excluded.instruction,
                    rationale=excluded.rationale,
                    confidence_score=excluded.confidence_score,
                    verification_count=excluded.verification_count,
                    status=excluded.status,
                    memory_tier=excluded.memory_tier,
                    conflicts_with=excluded.conflicts_with,
                    supersedes=excluded.supersedes,
                    skill_targets=excluded.skill_targets,
                    validator_results=excluded.validator_results,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
            """, (
                unit.id, unit.claim, unit.instruction, unit.rationale,
                unit.knowledge_type.value, unit.department.value, unit.scope,
                json.dumps(unit.applies_when), json.dumps(unit.does_not_apply_when),
                unit.source_type.value, unit.source_identifier, pos_json,
                unit.source_excerpt_hash, unit.source_reliability,
                unit.confidence_score, unit.verification_count,
                unit.last_confirmed_at.isoformat(),
                unit.sensitivity_level.value, int(unit.online_allowed),
                json.dumps(unit.tags), json.dumps(unit.entities),
                json.dumps(unit.tools_required),
                json.dumps(unit.related_units), json.dumps(unit.conflicts_with),
                json.dumps(unit.supersedes),
                unit.status.value, unit.memory_tier.value,
                json.dumps(unit.skill_targets),
                json.dumps(unit.validator_results),
                unit.created_at.isoformat(), now,
                unit.expires_at.isoformat() if unit.expires_at else None,
            ))

    def get_units_by_department(
        self, department: Department,
        status: Optional[UnitStatus] = None,
        min_confidence: float = 0.0,
    ) -> List[AtomicKnowledgeUnit]:
        """Get atomic units filtered by department, optionally by status and confidence."""
        query = "SELECT * FROM atomic_units WHERE department = ?"
        params: list = [department.value]

        if status:
            query += " AND status = ?"
            params.append(status.value)

        if min_confidence > 0.0:
            query += " AND confidence_score >= ?"
            params.append(min_confidence)

        query += " ORDER BY confidence_score DESC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_unit(r) for r in rows]

    def get_unit_by_id(self, unit_id: str) -> Optional[AtomicKnowledgeUnit]:
        """Get a single atomic unit by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM atomic_units WHERE id = ?", (unit_id,)
            ).fetchone()
        return self._row_to_unit(row) if row else None

    def get_approved_units_for_skill(
        self, department: Department, min_confidence: float = 0.7
    ) -> List[AtomicKnowledgeUnit]:
        """Get all approved units suitable for skill generation."""
        return self.get_units_by_department(
            department, status=UnitStatus.APPROVED, min_confidence=min_confidence
        )

    def get_conflicting_units(self, unit_id: str) -> List[AtomicKnowledgeUnit]:
        """Get all units that conflict with the given unit."""
        unit = self.get_unit_by_id(unit_id)
        if not unit or not unit.conflicts_with:
            return []
        placeholders = ",".join("?" * len(unit.conflicts_with))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM atomic_units WHERE id IN ({placeholders})",
                unit.conflicts_with
            ).fetchall()
        return [self._row_to_unit(r) for r in rows]

    def count_units(
        self, department: Optional[Department] = None,
        status: Optional[UnitStatus] = None
    ) -> int:
        """Count atomic units with optional filters."""
        query = "SELECT COUNT(*) FROM atomic_units WHERE 1=1"
        params: list = []
        if department:
            query += " AND department = ?"
            params.append(department.value)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return row[0]

    def _row_to_unit(self, row: sqlite3.Row) -> AtomicKnowledgeUnit:
        """Convert a SQLite row back to an AtomicKnowledgeUnit."""
        pos = None
        if row["source_position_json"]:
            pos = SourcePosition(**json.loads(row["source_position_json"]))

        # expires_at may not exist in v1 databases until migration runs
        expires_at_raw = None
        try:
            expires_at_raw = row["expires_at"]
        except (IndexError, KeyError):
            pass

        return AtomicKnowledgeUnit(
            id=row["id"],
            claim=row["claim"],
            instruction=row["instruction"],
            rationale=row["rationale"],
            knowledge_type=KnowledgeType(row["knowledge_type"]),
            department=Department(row["department"]),
            scope=row["scope"],
            applies_when=json.loads(row["applies_when"]),
            does_not_apply_when=json.loads(row["does_not_apply_when"]),
            source_type=SourceType(row["source_type"]),
            source_identifier=row["source_identifier"],
            source_position=pos,
            source_excerpt_hash=row["source_excerpt_hash"],
            source_reliability=row["source_reliability"],
            confidence_score=row["confidence_score"],
            verification_count=row["verification_count"],
            last_confirmed_at=datetime.fromisoformat(row["last_confirmed_at"]),
            sensitivity_level=SensitivityLevel(row["sensitivity_level"]),
            online_allowed=bool(row["online_allowed"]),
            tags=json.loads(row["tags"]),
            entities=json.loads(row["entities"]),
            tools_required=json.loads(row["tools_required"]),
            related_units=json.loads(row["related_units"]),
            conflicts_with=json.loads(row["conflicts_with"]),
            supersedes=json.loads(row["supersedes"]),
            status=UnitStatus(row["status"]),
            memory_tier=MemoryTier(row["memory_tier"]),
            skill_targets=json.loads(row["skill_targets"]),
            validator_results=json.loads(row["validator_results"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            expires_at=datetime.fromisoformat(expires_at_raw) if expires_at_raw else None,
        )

    # ═══════════════════════════════════════════════════════════════
    # SOURCE REFS (immutable, append-only)
    # ═══════════════════════════════════════════════════════════════

    def save_source_ref(self, ref: SourceRef):
        """Record an immutable source reference."""
        with self._connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO source_refs (
                    id, source_type, source_identifier, file_hash,
                    acquired_at, byte_size, privacy_mode, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ref.id, ref.source_type.value, ref.source_identifier,
                ref.file_hash, ref.acquired_at.isoformat(),
                ref.byte_size, ref.privacy_mode.value,
                json.dumps(ref.metadata),
            ))

    def get_source_ref(self, ref_id: str) -> Optional[SourceRef]:
        """Get a source reference by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_refs WHERE id = ?", (ref_id,)
            ).fetchone()
        if not row:
            return None
        return SourceRef(
            id=row["id"],
            source_type=SourceType(row["source_type"]),
            source_identifier=row["source_identifier"],
            file_hash=row["file_hash"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            byte_size=row["byte_size"],
            privacy_mode=PrivacyMode(row["privacy_mode"]),
            metadata=json.loads(row["metadata_json"]),
        )

    # ═══════════════════════════════════════════════════════════════
    # AUDIT LOG (append-only)
    # ═══════════════════════════════════════════════════════════════

    def log_audit(self, entry: AuditEntry):
        """Append an audit entry. Never modified or deleted."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO audit_log (
                    id, timestamp, action, actor,
                    target_type, target_id, details_json,
                    provider_used, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id, entry.timestamp.isoformat(),
                entry.action, entry.actor,
                entry.target_type, entry.target_id,
                json.dumps(entry.details),
                entry.provider_used, entry.cost_usd,
            ))

    def get_audit_trail(
        self, target_id: Optional[str] = None, limit: int = 50
    ) -> List[AuditEntry]:
        """Get audit entries, optionally filtered by target."""
        if target_id:
            query = "SELECT * FROM audit_log WHERE target_id = ? ORDER BY timestamp DESC LIMIT ?"
            params: tuple = (target_id, limit)
        else:
            query = "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?"
            params = (limit,)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [AuditEntry(
            id=r["id"],
            timestamp=datetime.fromisoformat(r["timestamp"]),
            action=r["action"],
            actor=r["actor"],
            target_type=r["target_type"],
            target_id=r["target_id"],
            details=json.loads(r["details_json"]),
            provider_used=r["provider_used"],
            cost_usd=r["cost_usd"],
        ) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # FAILURE MEMORY
    # ═══════════════════════════════════════════════════════════════

    def save_failure(
        self, failure_id: str, issue: str, correction: str,
        related_unit_id: Optional[str] = None,
        related_skill: Optional[str] = None,
        severity: str = "medium",
    ):
        """Record a failure or correction."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO failure_memory (
                    id, related_unit_id, related_skill,
                    issue_description, correction, severity,
                    reported_at, resolved, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, '{}')
            """, (
                failure_id, related_unit_id, related_skill,
                issue, correction, severity, now,
            ))

    def get_unresolved_failures(self) -> List[Dict[str, Any]]:
        """Get all unresolved failure records."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM failure_memory WHERE resolved = 0 ORDER BY reported_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # SESSION MEMORY (8th tier — per-conversation facts)
    # ═══════════════════════════════════════════════════════════════

    def save_session_fact(self, fact: SessionFact):
        """Save or update a session fact."""
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO session_memory (
                    id, agent_id, user_id, fact, fact_type, confidence,
                    source_conversation, created_at, expires_at,
                    promoted_to_unit_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fact.id, fact.agent_id, fact.user_id,
                fact.fact, fact.fact_type, fact.confidence,
                fact.source_conversation,
                fact.created_at.isoformat(),
                fact.expires_at.isoformat() if fact.expires_at else None,
                fact.promoted_to_unit_id,
            ))

    def get_session_facts(
        self,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        include_expired: bool = False,
        limit: int = 200,
    ) -> List[SessionFact]:
        """Get session facts, optionally filtered by agent/user."""
        query = "SELECT * FROM session_memory WHERE 1=1"
        params: list = []

        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if not include_expired:
            now_iso = datetime.now(timezone.utc).isoformat()
            query += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(now_iso)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_session_fact(r) for r in rows]

    def _row_to_session_fact(self, row: sqlite3.Row) -> SessionFact:
        """Convert a SQLite row to a SessionFact."""
        return SessionFact(
            id=row["id"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            fact=row["fact"],
            fact_type=row["fact_type"],
            confidence=row["confidence"],
            source_conversation=row["source_conversation"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            promoted_to_unit_id=row["promoted_to_unit_id"],
        )

    # ═══════════════════════════════════════════════════════════════
    # AUTOMATIC EXPIRY — Supermemory-style forgetting
    # ═══════════════════════════════════════════════════════════════

    def expire_stale_units(self, cutoff: Optional[datetime] = None) -> int:
        """Mark atomic units past their expires_at as EXPIRED.

        Returns the number of units expired.
        """
        if cutoff is None:
            cutoff = datetime.now(timezone.utc)
        cutoff_iso = cutoff.isoformat()

        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE atomic_units
                SET status = ?, updated_at = ?
                WHERE expires_at IS NOT NULL
                  AND expires_at <= ?
                  AND status NOT IN (?, ?, ?)
            """, (
                UnitStatus.EXPIRED.value, cutoff_iso, cutoff_iso,
                UnitStatus.EXPIRED.value, UnitStatus.RETIRED.value,
                UnitStatus.REJECTED.value,
            ))
            return cursor.rowcount

    def expire_stale_sessions(self, cutoff: Optional[datetime] = None) -> int:
        """Delete session facts past their expires_at.

        Returns the number of sessions cleaned up.
        """
        if cutoff is None:
            cutoff = datetime.now(timezone.utc)
        cutoff_iso = cutoff.isoformat()

        with self._connect() as conn:
            cursor = conn.execute("""
                DELETE FROM session_memory
                WHERE expires_at IS NOT NULL
                  AND expires_at <= ?
                  AND promoted_to_unit_id IS NULL
            """, (cutoff_iso,))
            return cursor.rowcount

    # ═══════════════════════════════════════════════════════════════
    # USER / AGENT PROFILE AGGREGATION
    # ═══════════════════════════════════════════════════════════════

    def get_user_profile(self, user_id: str) -> UserProfile:
        """Build an aggregated profile for a user from session facts + canonical units."""
        with self._connect() as conn:
            # Session facts for this user
            session_rows = conn.execute(
                "SELECT COUNT(*) as cnt, MAX(created_at) as last_at "
                "FROM session_memory WHERE user_id = ?",
                (user_id,)
            ).fetchone()

            # Expertise from session fact text (top mentioned departments)
            dept_rows = conn.execute("""
                SELECT fact_type, COUNT(*) as cnt
                FROM session_memory
                WHERE user_id = ?
                GROUP BY fact_type
                ORDER BY cnt DESC
            """, (user_id,)).fetchall()

            # Canonical contributions (units where source_identifier contains user_id)
            canonical_count = conn.execute(
                "SELECT COUNT(*) FROM atomic_units "
                "WHERE status = 'approved' AND source_identifier LIKE ?",
                (f"%{user_id}%",)
            ).fetchone()[0]

            # Top departments the user contributes to
            dept_contrib = conn.execute("""
                SELECT department, COUNT(*) as cnt
                FROM session_memory
                WHERE user_id = ?
                GROUP BY department
                ORDER BY cnt DESC
                LIMIT 5
            """, (user_id,)).fetchall() if False else []
            # Session memory doesn't have department — derive from facts
            # Use a simple approach: check if user has session facts at all
            departments: List[str] = []

            # Top knowledge types from facts
            top_types = [r["fact_type"] for r in dept_rows[:5]]

            session_count = session_rows["cnt"] if session_rows else 0
            last_active = session_rows["last_at"] if session_rows and session_rows["last_at"] else None

            # Profile confidence = normalized score based on data volume
            confidence = min(1.0, (session_count + canonical_count) / 50.0)

        return UserProfile(
            user_id=user_id,
            departments=departments,
            expertise_areas=top_types,
            session_fact_count=session_count,
            canonical_contribution_count=canonical_count,
            top_knowledge_types=top_types,
            last_active_at=datetime.fromisoformat(last_active) if last_active else None,
            profile_confidence=round(confidence, 3),
        )

    def get_agent_profile(self, agent_id: str) -> UserProfile:
        """Build an aggregated profile for an agent from session facts."""
        with self._connect() as conn:
            session_rows = conn.execute(
                "SELECT COUNT(*) as cnt, MAX(created_at) as last_at "
                "FROM session_memory WHERE agent_id = ?",
                (agent_id,)
            ).fetchone()

            # Fact types breakdown
            type_rows = conn.execute("""
                SELECT fact_type, COUNT(*) as cnt
                FROM session_memory
                WHERE agent_id = ?
                GROUP BY fact_type
                ORDER BY cnt DESC
            """, (agent_id,)).fetchall()

            # Canonical units created via this agent's session promotions
            canonical_count = conn.execute("""
                SELECT COUNT(*) FROM session_memory
                WHERE agent_id = ? AND promoted_to_unit_id IS NOT NULL
            """, (agent_id,)).fetchone()[0]

            session_count = session_rows["cnt"] if session_rows else 0
            last_active = session_rows["last_at"] if session_rows and session_rows["last_at"] else None
            top_types = [r["fact_type"] for r in type_rows[:5]]
            confidence = min(1.0, (session_count + canonical_count) / 50.0)

        return UserProfile(
            user_id=agent_id,
            departments=[],
            expertise_areas=top_types,
            session_fact_count=session_count,
            canonical_contribution_count=canonical_count,
            top_knowledge_types=top_types,
            last_active_at=datetime.fromisoformat(last_active) if last_active else None,
            profile_confidence=round(confidence, 3),
        )

