"""
Layer 11: Agent Registry — Central Control Plane

Manages AgentProfile CRUD in SQLite, generates A2A-compatible
AgentCards, and provides semantic agent selection for task routing.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

from core.models import AgentProfile, AgentRole


# ── Default Agent Definitions ────────────────────────────────────
# These are seeded on first run if the agents table is empty.

DEFAULT_AGENTS: List[Dict[str, Any]] = [
    {
        "agent_id": "orchestrator",
        "display_name": "Orchestrator Agent",
        "icon": "🎯",
        "role": "orchestrator",
        "department": "shared",
        "description": "Routes incoming tasks to the best specialist agent. Handles cross-department queries and escalations.",
        "tools_allowlist": ["cortex_search", "cortex_list_agents"],
        "tools_denylist": [],
        "can_delegate_to": ["eng-ops-agent", "sales-intel-agent", "conflict-reviewer", "synthesizer-agent"],
        "accepts_tasks_from": [],
        "auto_bind_departments": ["shared"],
        "sensitivity_ceiling": "internal",
        "max_context_tokens": 8000,
    },
    {
        "agent_id": "eng-ops-agent",
        "display_name": "Engineering Operations Agent",
        "icon": "💻",
        "role": "specialist",
        "department": "engineering",
        "description": "Handles deployment rules, migration SOPs, security policies, and coding standards for the engineering department.",
        "tools_allowlist": ["cortex_search", "github_read_pr", "cortex_read_canonical"],
        "tools_denylist": ["github_push", "notion_write", "slack_post"],
        "can_delegate_to": ["conflict-reviewer"],
        "accepts_tasks_from": ["orchestrator", "synthesizer-agent"],
        "auto_bind_departments": ["engineering"],
        "sensitivity_ceiling": "internal",
        "max_context_tokens": 12000,
    },
    {
        "agent_id": "sales-intel-agent",
        "display_name": "Sales Intelligence Agent",
        "icon": "📈",
        "role": "specialist",
        "department": "sales",
        "description": "Manages sales playbooks, customer context, pricing rules, and deal workflows.",
        "tools_allowlist": ["cortex_search", "notion_read"],
        "tools_denylist": ["github_read_pr", "github_push"],
        "can_delegate_to": [],
        "accepts_tasks_from": ["orchestrator"],
        "auto_bind_departments": ["sales"],
        "sensitivity_ceiling": "confidential",
        "max_context_tokens": 8000,
    },
    {
        "agent_id": "conflict-reviewer",
        "display_name": "Conflict Reviewer Agent",
        "icon": "⚖️",
        "role": "reviewer",
        "department": "shared",
        "description": "Resolves CONTESTED AtomicKnowledgeUnits flagged by Layer 9. Compares conflicting claims and recommends resolution.",
        "tools_allowlist": ["cortex_search", "cortex_approve_unit", "cortex_reject_unit"],
        "tools_denylist": [],
        "can_delegate_to": ["orchestrator"],
        "accepts_tasks_from": ["orchestrator", "synthesizer-agent", "eng-ops-agent"],
        "auto_bind_departments": [],
        "sensitivity_ceiling": "internal",
        "max_context_tokens": 6000,
    },
    {
        "agent_id": "synthesizer-agent",
        "display_name": "Synthesizer Agent",
        "icon": "🧪",
        "role": "synthesizer",
        "department": "shared",
        "description": "Triggers and monitors Layer 6 Skill compilation. Publishes reload events when new SKILL.md files are generated.",
        "tools_allowlist": ["cortex_run_synthesis", "cortex_read_canonical", "cortex_search"],
        "tools_denylist": [],
        "can_delegate_to": ["eng-ops-agent", "sales-intel-agent"],
        "accepts_tasks_from": ["orchestrator"],
        "auto_bind_departments": [],
        "sensitivity_ceiling": "internal",
        "max_context_tokens": 6000,
    },
    {
        "agent_id": "content-creator",
        "display_name": "Content Creator Agent",
        "icon": "✍️",
        "role": "specialist",
        "department": "marketing",
        "description": "Manages brand voice, content guidelines, campaign workflows, and marketing SOPs.",
        "tools_allowlist": ["cortex_search", "notion_read"],
        "tools_denylist": ["github_read_pr", "github_push"],
        "can_delegate_to": [],
        "accepts_tasks_from": ["orchestrator"],
        "auto_bind_departments": ["marketing"],
        "sensitivity_ceiling": "internal",
        "max_context_tokens": 8000,
    },
    {
        "agent_id": "sec-auditor",
        "display_name": "Security Auditor Agent",
        "icon": "🛡️",
        "role": "specialist",
        "department": "engineering",
        "description": "Reviews security policies, access controls, vulnerability patterns, and compliance rules.",
        "tools_allowlist": ["cortex_search", "cortex_read_canonical"],
        "tools_denylist": ["github_push", "slack_post", "notion_write"],
        "can_delegate_to": ["conflict-reviewer"],
        "accepts_tasks_from": ["orchestrator", "eng-ops-agent"],
        "auto_bind_departments": ["engineering"],
        "sensitivity_ceiling": "restricted",
        "online_llm_allowed": False,
        "max_context_tokens": 8000,
    },
]


class AgentRegistry:
    """
    Central control plane for agent lifecycle management.

    Stores AgentProfile records in SQLite, provides CRUD APIs,
    generates A2A-compatible AgentCards, and supports semantic
    agent selection for task routing.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db_file = os.path.join(db_path, "bhrm.db")
        self._init_tables()
        self._seed_defaults()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self):
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id         TEXT PRIMARY KEY,
                    display_name     TEXT NOT NULL,
                    icon             TEXT DEFAULT '🤖',
                    role             TEXT NOT NULL,
                    department       TEXT NOT NULL,
                    description      TEXT NOT NULL DEFAULT '',
                    profile_json     TEXT NOT NULL,
                    agent_card_json  TEXT,
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS agent_skill_files (
                    agent_id         TEXT REFERENCES agents(agent_id) ON DELETE CASCADE,
                    skill_file_path  TEXT NOT NULL,
                    context_file_path TEXT,
                    PRIMARY KEY (agent_id, skill_file_path)
                );

                CREATE TABLE IF NOT EXISTS agent_delegations (
                    id               TEXT PRIMARY KEY,
                    from_agent_id    TEXT,
                    to_agent_id      TEXT,
                    task_type        TEXT,
                    unit_id          TEXT,
                    timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    outcome          TEXT
                );

                CREATE TABLE IF NOT EXISTS skill_reload_events (
                    id               TEXT PRIMARY KEY,
                    agent_id         TEXT,
                    changed_file     TEXT,
                    reload_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    success          BOOLEAN DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS task_results (
                    task_id          TEXT PRIMARY KEY,
                    agent_id         TEXT,
                    status           TEXT DEFAULT 'pending',
                    result_text      TEXT,
                    error_text       TEXT,
                    received_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def _seed_defaults(self):
        """Seed default agents on first run (if table is empty)."""
        conn = self._get_conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            if count > 0:
                return

            print("  [AgentRegistry] Seeding default agents...")
            for agent_def in DEFAULT_AGENTS:
                profile = AgentProfile(**agent_def)
                self._insert_profile(conn, profile)
            conn.commit()
            print(f"  [AgentRegistry] Seeded {len(DEFAULT_AGENTS)} default agents")
        finally:
            conn.close()

    def _insert_profile(self, conn: sqlite3.Connection, profile: AgentProfile):
        """Insert or replace an agent profile."""
        profile_json = profile.model_dump_json()
        card_json = json.dumps(self._build_agent_card(profile))
        conn.execute("""
            INSERT OR REPLACE INTO agents
            (agent_id, display_name, icon, role, department, description,
             profile_json, agent_card_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            profile.agent_id, profile.display_name, profile.icon,
            profile.role.value, profile.department, profile.description,
            profile_json, card_json,
        ))

    # ── CRUD ──────────────────────────────────────────────────────

    def register(self, profile: AgentProfile) -> AgentProfile:
        """Store or update an agent profile."""
        conn = self._get_conn()
        try:
            self._insert_profile(conn, profile)
            conn.commit()
        finally:
            conn.close()
        return profile

    def get(self, agent_id: str) -> Optional[AgentProfile]:
        """Retrieve agent profile by ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT profile_json FROM agents WHERE agent_id = ?",
                (agent_id,)
            ).fetchone()
            if not row:
                return None
            return AgentProfile.model_validate_json(row["profile_json"])
        finally:
            conn.close()

    def list_all(self) -> List[AgentProfile]:
        """List all registered agents."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT profile_json FROM agents ORDER BY department, display_name"
            ).fetchall()
            return [AgentProfile.model_validate_json(r["profile_json"]) for r in rows]
        finally:
            conn.close()

    def list_by_department(self, dept: str) -> List[AgentProfile]:
        """Get all agents scoped to a department."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT profile_json FROM agents WHERE department = ?",
                (dept,)
            ).fetchall()
            return [AgentProfile.model_validate_json(r["profile_json"]) for r in rows]
        finally:
            conn.close()

    def delete(self, agent_id: str) -> bool:
        """Remove an agent. Returns True if deleted."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM agents WHERE agent_id = ?", (agent_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        finally:
            conn.close()

    # ── Skill Binding ─────────────────────────────────────────────

    def bind_skill(self, agent_id: str, skill_name: str) -> bool:
        """Bind a skill to an agent. Returns True if newly bound."""
        profile = self.get(agent_id)
        if not profile:
            return False

        if skill_name not in profile.bound_skills:
            profile.bound_skills.append(skill_name)
            profile.updated_at = datetime.now(timezone.utc)
            self.register(profile)
            return True
        return False

    def unbind_skill(self, agent_id: str, skill_name: str) -> bool:
        """Unbind a skill from an agent. Returns True if removed."""
        profile = self.get(agent_id)
        if not profile or skill_name not in profile.bound_skills:
            return False

        profile.bound_skills.remove(skill_name)
        profile.updated_at = datetime.now(timezone.utc)
        self.register(profile)
        return True

    def get_bound_skills(self, agent_id: str) -> List[str]:
        """Get all skills bound to an agent."""
        profile = self.get(agent_id)
        return profile.bound_skills if profile else []

    # ── Agent Selection ───────────────────────────────────────────

    def select_agent_for_task(
        self, task_text: str, dept_hint: Optional[str] = None
    ) -> Optional[AgentProfile]:
        """
        Two-pass agent selection:
          Pass 1: Filter by department (if hint given)
          Pass 2: Keyword-match against agent descriptions
        Falls back to orchestrator if no match.
        """
        if dept_hint:
            candidates = self.list_by_department(dept_hint)
            # Also include shared agents
            candidates += [a for a in self.list_by_department("shared")
                           if a.agent_id not in [c.agent_id for c in candidates]]
        else:
            candidates = self.list_all()

        if not candidates:
            return self.get("orchestrator")

        # Simple keyword scoring (no vector index dependency)
        task_words = set(task_text.lower().split())
        scored = []
        for agent in candidates:
            desc_words = set(agent.description.lower().split())
            dept_words = set(agent.department.lower().split())
            all_agent_words = desc_words | dept_words
            overlap = len(task_words & all_agent_words)
            score = overlap / max(len(task_words), 1)
            scored.append((score, agent))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_agent = scored[0]

        if best_score >= 0.15:  # Low threshold since keyword matching is coarse
            return best_agent

        return self.get("orchestrator")

    # ── A2A AgentCard ─────────────────────────────────────────────

    def _build_agent_card(self, profile: AgentProfile) -> dict:
        """Produce A2A-compatible AgentCard JSON."""
        skills = []
        for skill_name in profile.bound_skills:
            skills.append({
                "id": skill_name,
                "name": skill_name.replace("-", " ").title(),
                "description": f"Operational knowledge: {skill_name}",
            })

        return {
            "id": profile.agent_id,
            "name": profile.display_name,
            "description": profile.description,
            "url": f"http://localhost:8100/agents/{profile.agent_id}",
            "skills": skills,
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
        }

    def get_agent_card(self, agent_id: str) -> Optional[dict]:
        """Get the A2A AgentCard for an agent."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT agent_card_json FROM agents WHERE agent_id = ?",
                (agent_id,)
            ).fetchone()
            if not row or not row["agent_card_json"]:
                return None
            return json.loads(row["agent_card_json"])
        finally:
            conn.close()

    # ── Reload Events ─────────────────────────────────────────────

    def record_reload(self, agent_id: str, changed_file: str, success: bool = True):
        """Log a skill reload event."""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO skill_reload_events (id, agent_id, changed_file, success)
                VALUES (?, ?, ?, ?)
            """, (str(uuid.uuid4())[:12], agent_id, changed_file, success))
            conn.commit()
        finally:
            conn.close()

        # Also update the agent's last_reloaded_at
        profile = self.get(agent_id)
        if profile:
            profile.last_reloaded_at = datetime.now(timezone.utc)
            profile.context_ready = success
            self.register(profile)

    def get_reload_events(self, agent_id: Optional[str] = None, limit: int = 20) -> List[dict]:
        """Get recent reload events."""
        conn = self._get_conn()
        try:
            if agent_id:
                rows = conn.execute(
                    "SELECT * FROM skill_reload_events WHERE agent_id = ? ORDER BY reload_at DESC LIMIT ?",
                    (agent_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM skill_reload_events ORDER BY reload_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Delegation Log ────────────────────────────────────────────

    def log_delegation(
        self, from_agent_id: str, to_agent_id: str,
        task_type: str, unit_id: Optional[str] = None,
        outcome: str = "pending"
    ):
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO agent_delegations
                (id, from_agent_id, to_agent_id, task_type, unit_id, outcome)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (str(uuid.uuid4())[:12], from_agent_id, to_agent_id,
                  task_type, unit_id, outcome))
            conn.commit()
        finally:
            conn.close()

    # ── Task Results ─────────────────────────────────────────────

    def save_task_result(
        self, task_id: str, agent_id: str, status: str,
        result_text: Optional[str] = None, error_text: Optional[str] = None,
    ):
        """Store a task result from an external agent callback."""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO task_results
                (task_id, agent_id, status, result_text, error_text)
                VALUES (?, ?, ?, ?, ?)
            """, (task_id, agent_id, status, result_text, error_text))
            conn.commit()
        finally:
            conn.close()

    def get_task_result(self, task_id: str) -> Optional[dict]:
        """Retrieve a task result by ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM task_results WHERE task_id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_delegation_outcome(self, task_id: str, outcome: str):
        """Update the outcome of a delegation by task_id (stored as id)."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE agent_delegations SET outcome = ? WHERE id = ?",
                (outcome, task_id)
            )
            conn.commit()
        finally:
            conn.close()

    # ── Summary for API ───────────────────────────────────────────

    def get_summary(self) -> dict:
        """Dashboard-level summary of agent registry."""
        agents = self.list_all()
        by_role = {}
        for a in agents:
            by_role.setdefault(a.role.value, []).append(a.agent_id)

        total_skills_bound = sum(len(a.bound_skills) for a in agents)
        agents_with_context = sum(1 for a in agents if a.context_ready)

        return {
            "total_agents": len(agents),
            "agents_with_context_ready": agents_with_context,
            "total_skills_bound": total_skills_bound,
            "by_role": by_role,
        }
