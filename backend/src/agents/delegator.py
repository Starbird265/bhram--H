"""
Layer 11: Agent Delegator — A2A Task Handoff

Handles task delegation between agents following A2A patterns.
Validates bi-directional authorization:
  - Source agent's `can_delegate_to` list must include the target
  - Target agent's `accepts_tasks_from` list must include the source

Security Rules:
  1. No agent can self-promote (add itself to accepts_tasks_from)
  2. No agent can modify its own AgentProfile at runtime
  3. Both source and target must explicitly permit the delegation
"""

import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any, Dict

from core.models import AgentProfile

logger = logging.getLogger(__name__)


class DelegationNotPermittedError(Exception):
    """Raised when an agent attempts unauthorized task delegation."""
    pass


@dataclass
class DelegatedTask:
    """A task being handed off from one agent to another."""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    task_type: str = ""                    # e.g. "conflict_review", "skill_build"
    description: str = ""
    unit_id: Optional[str] = None          # Optional FK to AtomicKnowledgeUnit
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TaskResult:
    """Result of a delegated task execution."""
    task_id: str
    status: str = "pending"                # pending, success, failed, escalated
    result: Optional[str] = None
    completed_at: Optional[datetime] = None


class AgentDelegator:
    """
    Handles task handoffs between agents following A2A patterns.

    Validates that delegation is permitted by BOTH:
      - source agent's `can_delegate_to` list
      - target agent's `accepts_tasks_from` list
    """

    def __init__(self, registry):
        """
        Args:
            registry: AgentRegistry instance for looking up agents
                      and logging delegation events.
        """
        self.registry = registry

    def delegate(
        self,
        from_agent: AgentProfile,
        to_agent_id: str,
        task: DelegatedTask,
    ) -> TaskResult:
        """
        Delegate a task from one agent to another.

        Validates authorization, logs the delegation, and returns
        a TaskResult (async execution is handled by the caller).

        Args:
            from_agent: The agent initiating the delegation
            to_agent_id: ID of the target agent
            task: The task being delegated

        Returns:
            TaskResult with pending status (actual execution is async)

        Raises:
            DelegationNotPermittedError: If authorization fails
        """
        # ── Authorization Check ──────────────────────────────────
        # Rule 1: Source must explicitly list target in can_delegate_to
        if to_agent_id not in from_agent.can_delegate_to:
            raise DelegationNotPermittedError(
                f"Agent '{from_agent.agent_id}' cannot delegate to "
                f"'{to_agent_id}'. Not in can_delegate_to list."
            )

        # Rule 2: Target must exist
        target = self.registry.get(to_agent_id)
        if not target:
            raise DelegationNotPermittedError(
                f"Target agent '{to_agent_id}' not found in registry."
            )

        # Rule 3: Target must accept from source
        if from_agent.agent_id not in target.accepts_tasks_from:
            raise DelegationNotPermittedError(
                f"Agent '{to_agent_id}' does not accept tasks from "
                f"'{from_agent.agent_id}'. Not in accepts_tasks_from list."
            )

        # ── Log Delegation ───────────────────────────────────────
        self.registry.log_delegation(
            from_agent_id=from_agent.agent_id,
            to_agent_id=to_agent_id,
            task_type=task.task_type,
            unit_id=task.unit_id,
            outcome="dispatched",
        )

        logger.info(
            f"Task delegated: {from_agent.agent_id} → {to_agent_id} "
            f"(type: {task.task_type}, task_id: {task.task_id})"
        )

        # ── Return pending result ────────────────────────────────
        return TaskResult(
            task_id=task.task_id,
            status="dispatched",
        )

    def can_delegate(self, from_agent_id: str, to_agent_id: str) -> bool:
        """Check if delegation is permitted without executing it."""
        source = self.registry.get(from_agent_id)
        if not source:
            return False

        if to_agent_id not in source.can_delegate_to:
            return False

        target = self.registry.get(to_agent_id)
        if not target:
            return False

        return from_agent_id in target.accepts_tasks_from

    def get_delegation_targets(self, agent_id: str) -> list:
        """Get all agents that this agent can delegate to."""
        agent = self.registry.get(agent_id)
        if not agent:
            return []

        targets = []
        for target_id in agent.can_delegate_to:
            target = self.registry.get(target_id)
            if target and agent_id in target.accepts_tasks_from:
                targets.append({
                    "agent_id": target.agent_id,
                    "display_name": target.display_name,
                    "role": target.role.value,
                    "department": target.department,
                })
        return targets
