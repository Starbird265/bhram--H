"""
Tests for Session Memory (8th tier), Automatic Expiry, User Profiles,
Tacit Knowledge Detection, and Strategic Hardening.

Covers:
  1. Session CRUD (save, query by agent/user, filtering)
  2. Expiry sweep (units + sessions)
  3. Profile aggregation (user + agent)
  4. Tacit knowledge pattern detection
  5. Schema migration (v1 → v2 safety)
  6. Strategic hardening functions
  7. Session-to-canonical promotion
"""

import sys
import os
import uuid
import tempfile
import shutil
from datetime import datetime, timezone, timedelta

import pytest

# ── Path setup ──────────────────────────────────────────────────
BACKEND_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend", "src"))
sys.path.insert(0, BACKEND_SRC)

from core.models import (
    AtomicKnowledgeUnit, UnitStatus, MemoryTier, Department,
    KnowledgeType, SourceType, SensitivityLevel,
    SessionFact, UserProfile,
)
from storage.sqlite_store import SQLiteStore
from memory import MemoryManager


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Create a fresh SQLiteStore in a temp directory."""
    db_path = str(tmp_path / "test_db")
    store = SQLiteStore(db_path=db_path)
    return store


@pytest.fixture
def memory(tmp_db):
    """Create a MemoryManager backed by a temp store."""
    return MemoryManager(tmp_db)


def _make_unit(
    unit_id=None,
    claim="Test claim",
    department=Department.ENGINEERING,
    status=UnitStatus.APPROVED,
    tier=MemoryTier.CANONICAL,
    confidence=0.85,
    expires_at=None,
):
    """Helper to create a minimal AtomicKnowledgeUnit."""
    return AtomicKnowledgeUnit(
        id=unit_id or f"aku-{uuid.uuid4().hex[:12]}",
        claim=claim,
        instruction=claim,
        knowledge_type=KnowledgeType.SOP,
        department=department,
        source_type=SourceType.SLACK,
        source_identifier="#test",
        source_excerpt_hash=uuid.uuid4().hex[:16],
        confidence_score=confidence,
        status=status,
        memory_tier=tier,
        expires_at=expires_at,
    )


# ═══════════════════════════════════════════════════════════════
# 1. SESSION MEMORY CRUD
# ═══════════════════════════════════════════════════════════════

class TestSessionMemoryCRUD:
    """Test session fact save, query, and filtering."""

    def test_save_and_retrieve_session_fact(self, memory):
        """Save a fact and retrieve it."""
        fact = memory.save_session_fact(
            fact="User prefers dark mode in all dashboards.",
            agent_id="agent-001",
            user_id="user-gaurav",
            fact_type="explicit",
        )

        assert fact.id.startswith("sf-")
        assert fact.fact == "User prefers dark mode in all dashboards."
        assert fact.agent_id == "agent-001"
        assert fact.user_id == "user-gaurav"
        assert fact.expires_at is not None  # default TTL applied

        # Retrieve
        facts = memory.get_session_context(agent_id="agent-001")
        assert len(facts) == 1
        assert facts[0].fact == "User prefers dark mode in all dashboards."

    def test_filter_by_user_id(self, memory):
        """Facts should be filterable by user_id."""
        memory.save_session_fact(fact="Fact A", user_id="alice")
        memory.save_session_fact(fact="Fact B", user_id="bob")
        memory.save_session_fact(fact="Fact C", user_id="alice")

        alice_facts = memory.get_session_context(user_id="alice")
        assert len(alice_facts) == 2

        bob_facts = memory.get_session_context(user_id="bob")
        assert len(bob_facts) == 1

    def test_filter_by_agent_id(self, memory):
        """Facts should be filterable by agent_id."""
        memory.save_session_fact(fact="Fact X", agent_id="agent-a")
        memory.save_session_fact(fact="Fact Y", agent_id="agent-b")

        a_facts = memory.get_session_context(agent_id="agent-a")
        assert len(a_facts) == 1
        assert a_facts[0].fact == "Fact X"

    def test_fact_types(self, memory):
        """Support explicit, inferred, and tacit fact types."""
        memory.save_session_fact(fact="Explicit fact", fact_type="explicit")
        memory.save_session_fact(fact="Inferred fact", fact_type="inferred")
        memory.save_session_fact(fact="Tacit fact", fact_type="tacit", confidence=0.5)

        all_facts = memory.get_session_context()
        assert len(all_facts) == 3

        tacit = [f for f in all_facts if f.fact_type == "tacit"]
        assert len(tacit) == 1
        assert tacit[0].confidence == 0.5


# ═══════════════════════════════════════════════════════════════
# 2. AUTOMATIC EXPIRY
# ═══════════════════════════════════════════════════════════════

class TestAutomaticExpiry:
    """Test expiry sweep for both units and session facts."""

    def test_expire_stale_units(self, tmp_db):
        """Units past their expires_at should be marked EXPIRED."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(days=30)

        unit_expired = _make_unit(unit_id="expire-me", expires_at=past)
        unit_active = _make_unit(unit_id="keep-me", expires_at=future)
        unit_no_expiry = _make_unit(unit_id="no-expiry")

        tmp_db.save_unit(unit_expired)
        tmp_db.save_unit(unit_active)
        tmp_db.save_unit(unit_no_expiry)

        count = tmp_db.expire_stale_units()
        assert count == 1

        # Verify the expired unit
        expired = tmp_db.get_unit_by_id("expire-me")
        assert expired.status == UnitStatus.EXPIRED

        # Active should be untouched
        active = tmp_db.get_unit_by_id("keep-me")
        assert active.status == UnitStatus.APPROVED

        # No-expiry should be untouched
        no_exp = tmp_db.get_unit_by_id("no-expiry")
        assert no_exp.status == UnitStatus.APPROVED

    def test_expire_stale_sessions(self, tmp_db):
        """Expired session facts (non-promoted) should be deleted."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(days=7)

        # Expired fact
        fact_expired = SessionFact(
            id="sf-expired", fact="Old fact", fact_type="explicit",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
            expires_at=past,
        )
        # Active fact
        fact_active = SessionFact(
            id="sf-active", fact="Current fact", fact_type="explicit",
            created_at=datetime.now(timezone.utc),
            expires_at=future,
        )
        # Expired but promoted (should NOT be deleted)
        fact_promoted = SessionFact(
            id="sf-promoted", fact="Promoted fact", fact_type="explicit",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
            expires_at=past,
            promoted_to_unit_id="aku-12345",
        )

        tmp_db.save_session_fact(fact_expired)
        tmp_db.save_session_fact(fact_active)
        tmp_db.save_session_fact(fact_promoted)

        count = tmp_db.expire_stale_sessions()
        assert count == 1  # only the non-promoted expired fact

        remaining = tmp_db.get_session_facts(include_expired=True)
        ids = [f.id for f in remaining]
        assert "sf-expired" not in ids
        assert "sf-active" in ids
        assert "sf-promoted" in ids  # promoted facts are preserved

    def test_memory_manager_expiry_sweep(self, memory, tmp_db):
        """MemoryManager.run_expiry_sweep() should clean both units and sessions."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)

        # Add an expired unit
        unit = _make_unit(unit_id="sweep-unit", expires_at=past)
        tmp_db.save_unit(unit)

        # Add an expired session
        fact = SessionFact(
            id="sf-sweep", fact="Sweep me", fact_type="explicit",
            created_at=past - timedelta(days=1), expires_at=past,
        )
        tmp_db.save_session_fact(fact)

        result = memory.run_expiry_sweep()
        assert result["expired_units"] == 1
        assert result["expired_sessions"] == 1
        assert result["total_cleaned"] == 2

    def test_expired_sessions_excluded_by_default(self, memory):
        """get_session_context should exclude expired facts by default."""
        # Save a fact with 0-day TTL (already expired, but let's use past directly)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        fact = SessionFact(
            id="sf-gone", fact="Already expired", fact_type="explicit",
            agent_id="agent-x",
            created_at=past - timedelta(days=1), expires_at=past,
        )
        memory._store.save_session_fact(fact)

        # Also save a valid one
        memory.save_session_fact(fact="Still valid", agent_id="agent-x")

        context = memory.get_session_context(agent_id="agent-x")
        assert len(context) == 1
        assert context[0].fact == "Still valid"


# ═══════════════════════════════════════════════════════════════
# 3. USER / AGENT PROFILES
# ═══════════════════════════════════════════════════════════════

class TestProfiles:
    """Test profile aggregation from session facts and canonical units."""

    def test_user_profile(self, memory):
        """Build a user profile from session facts."""
        memory.save_session_fact(fact="Uses vim", user_id="gaurav", fact_type="explicit")
        memory.save_session_fact(fact="Prefers dark mode", user_id="gaurav", fact_type="inferred")
        memory.save_session_fact(fact="We always test first", user_id="gaurav", fact_type="tacit")

        profile = memory.get_user_profile("gaurav")
        assert profile.user_id == "gaurav"
        assert profile.session_fact_count == 3
        assert profile.profile_confidence > 0.0
        assert isinstance(profile.expertise_areas, list)

    def test_agent_profile(self, memory):
        """Build an agent profile from session facts."""
        memory.save_session_fact(fact="Agent processed claim A", agent_id="cortex-1")
        memory.save_session_fact(fact="Agent processed claim B", agent_id="cortex-1")

        profile = memory.get_agent_profile("cortex-1")
        assert profile.user_id == "cortex-1"
        assert profile.session_fact_count == 2

    def test_empty_profile(self, memory):
        """Profile for unknown user should return zero counts."""
        profile = memory.get_user_profile("nobody")
        assert profile.session_fact_count == 0
        assert profile.canonical_contribution_count == 0
        assert profile.profile_confidence == 0.0


# ═══════════════════════════════════════════════════════════════
# 4. TACIT KNOWLEDGE DETECTION
# ═══════════════════════════════════════════════════════════════

class TestTacitKnowledge:
    """Test pattern-based tacit knowledge detection."""

    def test_detect_we_always_pattern(self):
        from core.strategic_hardening import detect_tacit_patterns

        facts = [
            {"id": "1", "fact": "We always run tests before deploying to prod."},
            {"id": "2", "fact": "The deploy script is in /opt/deploy.sh."},
        ]

        tacit = detect_tacit_patterns(facts)
        assert len(tacit) == 1
        assert tacit[0]["fact_type"] == "tacit"
        assert tacit[0]["id"] == "1"
        assert tacit[0]["tacit_confidence"] > 0.0

    def test_detect_everyone_knows_pattern(self):
        from core.strategic_hardening import detect_tacit_patterns

        facts = [
            {"id": "1", "fact": "Everyone knows you need to restart after a config change."},
        ]

        tacit = detect_tacit_patterns(facts)
        assert len(tacit) == 1

    def test_detect_unwritten_rule(self):
        from core.strategic_hardening import detect_tacit_patterns

        facts = [
            {"id": "1", "fact": "It's an unwritten rule that we never deploy on Fridays."},
        ]

        tacit = detect_tacit_patterns(facts)
        assert len(tacit) == 1
        assert tacit[0]["tacit_confidence"] >= 0.7

    def test_no_false_positives(self):
        from core.strategic_hardening import detect_tacit_patterns

        facts = [
            {"id": "1", "fact": "Use terraform plan before apply."},
            {"id": "2", "fact": "The database is PostgreSQL 15."},
        ]

        tacit = detect_tacit_patterns(facts)
        assert len(tacit) == 0

    def test_short_text_ignored(self):
        from core.strategic_hardening import detect_tacit_patterns

        facts = [
            {"id": "1", "fact": "We always"},  # too short
        ]

        tacit = detect_tacit_patterns(facts)
        assert len(tacit) == 0


# ═══════════════════════════════════════════════════════════════
# 5. SCHEMA MIGRATION v1 → v2
# ═══════════════════════════════════════════════════════════════

class TestSchemaMigration:
    """Verify that schema migration is safe and backward-compatible."""

    def test_schema_version_is_2(self, tmp_db):
        """New databases should be version 2."""
        with tmp_db._connect() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        assert row is not None
        assert row["value"] == "2"

    def test_session_memory_table_exists(self, tmp_db):
        """session_memory table should be created by init."""
        with tmp_db._connect() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        table_names = [t["name"] for t in tables]
        assert "session_memory" in table_names

    def test_expires_at_column_exists(self, tmp_db):
        """atomic_units should have an expires_at column."""
        unit = _make_unit(expires_at=datetime.now(timezone.utc) + timedelta(days=7))
        tmp_db.save_unit(unit)

        retrieved = tmp_db.get_unit_by_id(unit.id)
        assert retrieved.expires_at is not None

    def test_unit_without_expires_at(self, tmp_db):
        """Units without expires_at should still work (None)."""
        unit = _make_unit()
        tmp_db.save_unit(unit)

        retrieved = tmp_db.get_unit_by_id(unit.id)
        assert retrieved.expires_at is None


# ═══════════════════════════════════════════════════════════════
# 6. STRATEGIC HARDENING
# ═══════════════════════════════════════════════════════════════

class TestStrategicHardening:
    """Test strategic hardening functions."""

    def test_sovereignty_report(self, tmp_db):
        """Sovereignty report should return structured data."""
        from core.strategic_hardening import get_data_sovereignty_report

        # Need to figure out the db_path from tmp_db
        db_path = str(tmp_db.db_dir)
        report = get_data_sovereignty_report(db_path)

        assert "sovereignty_status" in report
        assert report["sovereignty_status"] == "COMPLIANT"
        assert "differentiators" in report

    def test_sqlite_health(self, tmp_db):
        """SQLite health check should return structured data."""
        from core.strategic_hardening import check_sqlite_health

        db_path = str(tmp_db.db_dir)
        health = check_sqlite_health(db_path)

        assert "health_score" in health
        assert "migration_recommended" in health
        assert "row_counts" in health
        assert health["migration_recommended"] is False  # tiny test DB

    def test_roi_metrics(self, tmp_db):
        """ROI calculator should return structured data."""
        from core.strategic_hardening import calculate_roi_metrics

        db_path = str(tmp_db.db_dir)
        roi = calculate_roi_metrics(db_path)

        assert "knowledge_metrics" in roi
        assert "cost_metrics" in roi
        assert "governance_metrics" in roi
        assert "value_summary" in roi


# ═══════════════════════════════════════════════════════════════
# 7. SESSION → CANONICAL PROMOTION
# ═══════════════════════════════════════════════════════════════

class TestSessionPromotion:
    """Test promoting session facts to canonical memory."""

    def test_promote_session_to_canonical(self, memory):
        """A session fact should become a canonical AtomicKnowledgeUnit."""
        fact = memory.save_session_fact(
            fact="Always run integration tests before merging.",
            agent_id="agent-001",
            user_id="gaurav",
        )

        unit = memory.promote_session_to_canonical(fact.id)

        assert unit is not None
        assert unit.status == UnitStatus.APPROVED
        assert unit.memory_tier == MemoryTier.CANONICAL
        assert unit.claim == "Always run integration tests before merging."

        # Verify the fact is marked as promoted
        facts = memory._store.get_session_facts(include_expired=True)
        promoted = [f for f in facts if f.id == fact.id]
        assert len(promoted) == 1
        assert promoted[0].promoted_to_unit_id == unit.id

    def test_promote_nonexistent_fact(self, memory):
        """Promoting a non-existent fact should return None."""
        result = memory.promote_session_to_canonical("sf-doesnotexist")
        assert result is None

    def test_promoted_facts_survive_expiry(self, memory, tmp_db):
        """Promoted session facts should not be deleted by expiry sweep."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)

        fact = SessionFact(
            id="sf-promoted-test",
            fact="Promoted and expired",
            fact_type="explicit",
            created_at=past - timedelta(days=1),
            expires_at=past,
            promoted_to_unit_id="aku-some-unit",
        )
        tmp_db.save_session_fact(fact)

        count = tmp_db.expire_stale_sessions()
        assert count == 0  # promoted fact should be preserved


# ═══════════════════════════════════════════════════════════════
# 8. ENUM EXTENSIONS
# ═══════════════════════════════════════════════════════════════

class TestEnumExtensions:
    """Verify new enum values exist and are usable."""

    def test_memory_tier_session(self):
        assert MemoryTier.SESSION == "session"
        assert MemoryTier.SESSION.value == "session"

    def test_unit_status_expired(self):
        assert UnitStatus.EXPIRED == "expired"
        assert UnitStatus.EXPIRED.value == "expired"

    def test_all_memory_tiers(self):
        """8 tiers should exist."""
        tiers = list(MemoryTier)
        assert len(tiers) == 8
        tier_names = {t.value for t in tiers}
        expected = {"working", "source", "canonical", "failure",
                    "vector", "skill", "audit", "session"}
        assert tier_names == expected

    def test_all_unit_statuses(self):
        """7 statuses should exist (including EXPIRED)."""
        statuses = list(UnitStatus)
        assert len(statuses) == 7
        assert "expired" in {s.value for s in statuses}


# ═══════════════════════════════════════════════════════════════
# 9. MEMORY MANAGER STATS
# ═══════════════════════════════════════════════════════════════

class TestMemoryStats:
    """Test that get_stats() includes new tier counts."""

    def test_stats_include_session_and_expired(self, memory, tmp_db):
        """Stats should include session_facts and atomic_units_expired counts."""
        # Add a session fact
        memory.save_session_fact(fact="Test fact", user_id="tester")

        # Add an expired unit
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        unit = _make_unit(unit_id="stat-expired", expires_at=past)
        tmp_db.save_unit(unit)
        tmp_db.expire_stale_units()

        stats = memory.get_stats()

        assert "session_facts" in stats
        assert stats["session_facts"] == 1
        assert "atomic_units_expired" in stats
        assert stats["atomic_units_expired"] == 1
