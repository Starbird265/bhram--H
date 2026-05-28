"""
Memory Manager — Unified interface to the 8-tier memory architecture.

Tiers:
  WORKING   → Temporary state for current pipeline run
  SOURCE    → Immutable source references (where data came from)
  CANONICAL → Approved operational knowledge (atomic units)
  FAILURE   → Corrections, "never do this" rules
  VECTOR    → Embedding index for semantic search
  SKILL     → Compiled skill documents + versions
  AUDIT     → Decision trail, who approved what
  SESSION   → Per-conversation facts from agent/user interactions

All tiers are backed by the SQLiteStore. This manager provides
tier-aware CRUD and cross-tier queries.
"""

import uuid
import hashlib
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from core.models import (
    AtomicKnowledgeUnit, KnowledgeChunk, SourceRef, AuditEntry,
    Department, KnowledgeType, UnitStatus, MemoryTier,
    SensitivityLevel, PrivacyMode, SkillDef,
    SessionFact, UserProfile, SourceType,
)
from storage.sqlite_store import SQLiteStore


# Default TTL for session facts (7 days)
SESSION_DEFAULT_TTL_DAYS = 7


class MemoryManager:
    """Unified interface to all 8 memory tiers."""

    def __init__(self, store: SQLiteStore):
        self._store = store

    # ─── WORKING MEMORY ──────────────────────────────────────────
    # Temporary storage for in-flight pipeline data

    def save_working_unit(self, unit: AtomicKnowledgeUnit) -> AtomicKnowledgeUnit:
        """Save a unit to working memory (temp, current run only)."""
        unit.memory_tier = MemoryTier.WORKING
        self._store.save_unit(unit)
        return unit

    def get_working_units(self) -> List[AtomicKnowledgeUnit]:
        """Get all units currently in working memory."""
        # Query by memory tier
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM atomic_units WHERE memory_tier = ?",
                (MemoryTier.WORKING.value,)
            ).fetchall()
        return [self._store._row_to_unit(r) for r in rows]

    def flush_working_memory(self):
        """Clear all working memory entries (end of pipeline run)."""
        with self._store._connect() as conn:
            conn.execute(
                "DELETE FROM atomic_units WHERE memory_tier = ?",
                (MemoryTier.WORKING.value,)
            )

    # ─── SOURCE MEMORY (immutable) ───────────────────────────────
    # Records where data came from — never modified after creation

    def record_source(self, ref: SourceRef) -> SourceRef:
        """Record an immutable source reference."""
        self._store.save_source_ref(ref)
        return ref

    def get_source(self, ref_id: str) -> Optional[SourceRef]:
        """Look up a source reference."""
        return self._store.get_source_ref(ref_id)

    def get_all_sources(self) -> List[SourceRef]:
        """Get all recorded source references."""
        with self._store._connect() as conn:
            rows = conn.execute("SELECT * FROM source_refs ORDER BY acquired_at DESC").fetchall()
        results = []
        for r in rows:
            results.append(SourceRef(
                id=r["id"],
                source_type=r["source_type"],
                source_identifier=r["source_identifier"],
                file_hash=r["file_hash"],
                acquired_at=datetime.fromisoformat(r["acquired_at"]),
                byte_size=r["byte_size"],
                privacy_mode=PrivacyMode(r["privacy_mode"]),
                metadata=__import__("json").loads(r["metadata_json"]),
            ))
        return results

    # ─── CANONICAL MEMORY ────────────────────────────────────────
    # Approved operational knowledge — the gold standard

    def promote_to_canonical(self, unit: AtomicKnowledgeUnit) -> AtomicKnowledgeUnit:
        """Promote a working/candidate unit to canonical memory."""
        unit.status = UnitStatus.APPROVED
        unit.memory_tier = MemoryTier.CANONICAL
        unit.updated_at = datetime.now(timezone.utc)
        self._store.save_unit(unit)

        # Audit the promotion
        self.log_decision(
            action="promote_canonical",
            target_type="unit",
            target_id=unit.id,
            details={"department": unit.department.value, "confidence": unit.confidence_score},
        )
        return unit

    def get_canonical_units(
        self,
        department: Optional[Department] = None,
        knowledge_type: Optional[KnowledgeType] = None,
        min_confidence: float = 0.0,
    ) -> List[AtomicKnowledgeUnit]:
        """Get approved canonical knowledge, with optional filters."""
        query = "SELECT * FROM atomic_units WHERE memory_tier = ? AND status = ?"
        params: list = [MemoryTier.CANONICAL.value, UnitStatus.APPROVED.value]

        if department:
            query += " AND department = ?"
            params.append(department.value)
        if knowledge_type:
            query += " AND knowledge_type = ?"
            params.append(knowledge_type.value)
        if min_confidence > 0.0:
            query += " AND confidence_score >= ?"
            params.append(min_confidence)

        query += " ORDER BY confidence_score DESC"

        with self._store._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._store._row_to_unit(r) for r in rows]

    def supersede_unit(self, old_id: str, new_unit: AtomicKnowledgeUnit):
        """Mark an old unit as superseded and link the new one."""
        old = self._store.get_unit_by_id(old_id)
        if old:
            old.status = UnitStatus.SUPERSEDED
            old.updated_at = datetime.now(timezone.utc)
            self._store.save_unit(old)

        new_unit.supersedes = list(set(new_unit.supersedes + [old_id]))
        self._store.save_unit(new_unit)

        self.log_decision(
            action="supersede",
            target_type="unit",
            target_id=old_id,
            details={"replaced_by": new_unit.id},
        )

    # ─── FAILURE MEMORY ──────────────────────────────────────────
    # Corrections, incidents, "never do this" patterns

    def record_failure(
        self,
        issue: str,
        correction: str,
        related_unit_id: Optional[str] = None,
        related_skill: Optional[str] = None,
        severity: str = "medium",
    ) -> str:
        """Record a failure or correction."""
        failure_id = f"fail-{uuid.uuid4().hex[:12]}"
        self._store.save_failure(
            failure_id, issue, correction,
            related_unit_id=related_unit_id,
            related_skill=related_skill,
            severity=severity,
        )

        self.log_decision(
            action="record_failure",
            target_type="failure",
            target_id=failure_id,
            details={"severity": severity, "related_unit": related_unit_id},
        )
        return failure_id

    def get_active_failures(self) -> List[Dict[str, Any]]:
        """Get all unresolved failures."""
        return self._store.get_unresolved_failures()

    # ─── SKILL MEMORY ────────────────────────────────────────────
    # Compiled skill documents (stored as chunks with layer=SYNTHESIZED)

    def get_skills_for_department(self, department: Department) -> List[KnowledgeChunk]:
        """Get all synthesized skill chunks for a department."""
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE department = ? AND processing_layer = ?",
                (department.value, "synthesized")
            ).fetchall()
        return [self._store._row_to_chunk(r) for r in rows]

    # ─── AUDIT MEMORY (append-only) ──────────────────────────────
    # Every decision the system makes

    def log_decision(
        self,
        action: str,
        target_type: str,
        target_id: str,
        actor: str = "system",
        details: Optional[Dict[str, Any]] = None,
        provider_used: Optional[str] = None,
        cost_usd: Optional[float] = None,
    ):
        """Log a decision to the audit trail."""
        entry = AuditEntry(
            id=f"audit-{uuid.uuid4().hex[:12]}",
            action=action,
            actor=actor,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
            provider_used=provider_used,
            cost_usd=cost_usd,
        )
        self._store.log_audit(entry)

    def get_audit_trail(
        self, target_id: Optional[str] = None, limit: int = 50
    ) -> List[AuditEntry]:
        """Get audit trail entries."""
        return self._store.get_audit_trail(target_id=target_id, limit=limit)

    # ─── SESSION MEMORY (8th tier) ───────────────────────────────
    # Per-conversation facts from agent/user interactions

    def save_session_fact(
        self,
        fact: str,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        fact_type: str = "explicit",
        confidence: float = 0.8,
        source_conversation: Optional[str] = None,
        ttl_days: Optional[int] = None,
    ) -> SessionFact:
        """Save a session fact with optional TTL.

        Args:
            fact: The fact text (1-2 sentences).
            agent_id: Which agent captured this fact.
            user_id: Which user the fact is about.
            fact_type: 'explicit', 'inferred', or 'tacit'.
            confidence: How confident we are (0.0–1.0).
            source_conversation: Session/conversation ID.
            ttl_days: Days until expiry. None = never expires.
                      Default SESSION_DEFAULT_TTL_DAYS used when not specified.
        """
        if ttl_days is None:
            ttl_days = SESSION_DEFAULT_TTL_DAYS

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=ttl_days) if ttl_days > 0 else None

        session_fact = SessionFact(
            id=f"sf-{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            user_id=user_id,
            fact=fact,
            fact_type=fact_type,
            confidence=confidence,
            source_conversation=source_conversation,
            created_at=now,
            expires_at=expires,
        )

        self._store.save_session_fact(session_fact)

        self.log_decision(
            action="save_session_fact",
            target_type="session_fact",
            target_id=session_fact.id,
            details={
                "agent_id": agent_id,
                "user_id": user_id,
                "fact_type": fact_type,
                "ttl_days": ttl_days,
            },
        )

        return session_fact

    def get_session_context(
        self,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[SessionFact]:
        """Get active (non-expired) session facts for an agent/user."""
        return self._store.get_session_facts(
            agent_id=agent_id, user_id=user_id, include_expired=False
        )

    def promote_session_to_canonical(
        self,
        fact_id: str,
        department: Department = Department.SHARED,
        knowledge_type: KnowledgeType = KnowledgeType.PREFERENCE,
    ) -> Optional[AtomicKnowledgeUnit]:
        """Promote a session fact to a canonical AtomicKnowledgeUnit.

        Returns the new unit, or None if the fact doesn't exist.
        """
        # Find the session fact
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_memory WHERE id = ?", (fact_id,)
            ).fetchone()

        if not row:
            return None

        fact = self._store._row_to_session_fact(row)

        # Create a canonical unit from the fact
        _claim_key = f"{fact.fact.strip().lower()}|{department.value}"
        _det_id = f"aku-{hashlib.sha256(_claim_key.encode()).hexdigest()[:12]}"

        unit = AtomicKnowledgeUnit(
            id=_det_id,
            claim=fact.fact,
            instruction=fact.fact,
            knowledge_type=knowledge_type,
            department=department,
            source_type=SourceType.LOCAL_FILE,
            source_identifier=f"session:{fact.agent_id or fact.user_id or 'unknown'}",
            source_excerpt_hash=hashlib.sha256(fact.fact.encode()).hexdigest()[:16],
            confidence_score=fact.confidence,
            status=UnitStatus.APPROVED,
            memory_tier=MemoryTier.CANONICAL,
        )

        self._store.save_unit(unit)

        # Mark the session fact as promoted
        fact.promoted_to_unit_id = unit.id
        self._store.save_session_fact(fact)

        self.log_decision(
            action="promote_session_to_canonical",
            target_type="session_fact",
            target_id=fact_id,
            details={"promoted_to": unit.id, "department": department.value},
        )

        return unit

    # ─── AUTOMATIC EXPIRY ────────────────────────────────────────
    # Supermemory-style forgetting of stale knowledge

    def run_expiry_sweep(self) -> Dict[str, int]:
        """Run the expiry sweep: expire stale units + delete expired sessions.

        Returns a summary of what was cleaned.
        """
        expired_units = self._store.expire_stale_units()
        expired_sessions = self._store.expire_stale_sessions()

        total = expired_units + expired_sessions

        if total > 0:
            self.log_decision(
                action="expiry_sweep",
                target_type="system",
                target_id="expiry_sweep",
                details={
                    "expired_units": expired_units,
                    "expired_sessions": expired_sessions,
                },
            )

        return {
            "expired_units": expired_units,
            "expired_sessions": expired_sessions,
            "total_cleaned": total,
        }

    # ─── USER / AGENT PROFILES ───────────────────────────────────
    # Aggregated profiles from session facts + canonical contributions

    def get_user_profile(self, user_id: str) -> UserProfile:
        """Get an aggregated profile for a specific user."""
        return self._store.get_user_profile(user_id)

    def get_agent_profile(self, agent_id: str) -> UserProfile:
        """Get an aggregated profile for a specific agent."""
        return self._store.get_agent_profile(agent_id)

    # ─── CROSS-TIER QUERIES ──────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics across all 8 memory tiers."""
        with self._store._connect() as conn:
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            units_total = conn.execute("SELECT COUNT(*) FROM atomic_units").fetchone()[0]
            units_canonical = conn.execute(
                "SELECT COUNT(*) FROM atomic_units WHERE memory_tier = ?",
                (MemoryTier.CANONICAL.value,)
            ).fetchone()[0]
            units_working = conn.execute(
                "SELECT COUNT(*) FROM atomic_units WHERE memory_tier = ?",
                (MemoryTier.WORKING.value,)
            ).fetchone()[0]
            units_expired = conn.execute(
                "SELECT COUNT(*) FROM atomic_units WHERE status = ?",
                (UnitStatus.EXPIRED.value,)
            ).fetchone()[0]
            sources = conn.execute("SELECT COUNT(*) FROM source_refs").fetchone()[0]
            audits = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            failures = conn.execute(
                "SELECT COUNT(*) FROM failure_memory WHERE resolved = 0"
            ).fetchone()[0]
            session_facts = conn.execute(
                "SELECT COUNT(*) FROM session_memory"
            ).fetchone()[0]

        return {
            "chunks": chunks,
            "atomic_units_total": units_total,
            "atomic_units_canonical": units_canonical,
            "atomic_units_working": units_working,
            "atomic_units_expired": units_expired,
            "source_refs": sources,
            "audit_entries": audits,
            "unresolved_failures": failures,
            "session_facts": session_facts,
        }
