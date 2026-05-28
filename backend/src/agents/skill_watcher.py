"""
Layer 11: Skill File Watcher + Agent Reload Bus

Monitors SKILL.md and CONTEXT.md files for changes and triggers
hot-reload of affected agents' context assemblies.

Architecture:
  Layer 6 Synthesis writes SKILL.md / CONTEXT.md
    → SkillFileWatcher detects the write (watchdog)
    → AgentReloadBus notifies affected agents
    → Context is re-assembled for each agent
"""

import os
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Callable, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class AgentReloadEvent:
    """Event fired when a skill file changes and an agent must reload."""
    agent_id: str
    changed_file: str
    timestamp: float = field(default_factory=time.time)
    skill_name: Optional[str] = None


class AgentReloadBus:
    """
    In-process pub/sub for agent reload events.

    Agents subscribe with a handler callback. When a skill file
    changes, the bus publishes to all affected agents.
    """

    def __init__(self):
        self._handlers: Dict[str, Callable[[AgentReloadEvent], None]] = {}
        self._log: List[AgentReloadEvent] = []

    def subscribe(self, agent_id: str, handler: Callable[[AgentReloadEvent], None]):
        """Register a reload handler for an agent."""
        self._handlers[agent_id] = handler

    def unsubscribe(self, agent_id: str):
        """Remove an agent's reload handler."""
        self._handlers.pop(agent_id, None)

    def publish(self, event: AgentReloadEvent):
        """
        Dispatch a reload event.

        If agent_id is '*', broadcast to all subscribers.
        Otherwise, dispatch to the specific agent.
        """
        self._log.append(event)

        if event.agent_id == "*":
            # Broadcast to all
            for agent_id, handler in self._handlers.items():
                try:
                    handler(AgentReloadEvent(
                        agent_id=agent_id,
                        changed_file=event.changed_file,
                        timestamp=event.timestamp,
                        skill_name=event.skill_name,
                    ))
                except Exception as e:
                    logger.error(f"Reload handler error for {agent_id}: {e}")
        else:
            handler = self._handlers.get(event.agent_id)
            if handler:
                try:
                    handler(event)
                except Exception as e:
                    logger.error(f"Reload handler error for {event.agent_id}: {e}")

    def get_recent_events(self, limit: int = 20) -> List[AgentReloadEvent]:
        """Get recent reload events for dashboard display."""
        return self._log[-limit:]


class SkillFileWatcher:
    """
    Watches a directory for SKILL.md and CONTEXT.md changes using
    polling (no external dependency on watchdog — uses stdlib).

    On change detection:
      1. Debounce (configurable, default 5s)
      2. Find affected agents via registry
      3. Publish AgentReloadEvent to the bus
    """

    def __init__(
        self,
        watch_dir: str,
        reload_bus: AgentReloadBus,
        registry=None,
        debounce_seconds: int = 5,
    ):
        self.watch_dir = watch_dir
        self.reload_bus = reload_bus
        self.registry = registry
        self.debounce_seconds = debounce_seconds

        self._last_modified: Dict[str, float] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start watching in a background thread."""
        if self._running:
            return

        self._running = True
        # Take initial snapshot
        self._snapshot()

        self._thread = threading.Thread(
            target=self._watch_loop,
            daemon=True,
            name="SkillFileWatcher",
        )
        self._thread.start()
        logger.info(f"SkillFileWatcher started on: {self.watch_dir}")

    def stop(self):
        """Stop the watcher."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _snapshot(self):
        """Take a snapshot of all SKILL.md and CONTEXT.md modification times."""
        if not os.path.isdir(self.watch_dir):
            return

        for root, dirs, files in os.walk(self.watch_dir):
            for fname in files:
                if fname in ("SKILL.md", "CONTEXT.md"):
                    fpath = os.path.join(root, fname)
                    try:
                        self._last_modified[fpath] = os.path.getmtime(fpath)
                    except OSError:
                        pass

    def _watch_loop(self):
        """Poll for file changes every `debounce_seconds`."""
        while self._running:
            time.sleep(self.debounce_seconds)

            if not os.path.isdir(self.watch_dir):
                continue

            for root, dirs, files in os.walk(self.watch_dir):
                for fname in files:
                    if fname not in ("SKILL.md", "CONTEXT.md"):
                        continue

                    fpath = os.path.join(root, fname)
                    try:
                        current_mtime = os.path.getmtime(fpath)
                    except OSError:
                        continue

                    last_mtime = self._last_modified.get(fpath, 0)
                    if current_mtime > last_mtime:
                        self._last_modified[fpath] = current_mtime
                        self._on_file_changed(fpath)

    def _on_file_changed(self, file_path: str):
        """Handle a detected file change."""
        # Extract skill name from path: .../department/skill-name/SKILL.md
        parts = file_path.replace("\\", "/").split("/")
        skill_name = None
        if len(parts) >= 2:
            # The parent dir of SKILL.md is the skill name
            skill_name = parts[-2]

        logger.info(f"Skill file changed: {file_path} (skill: {skill_name})")

        # Find affected agents
        affected_agent_ids: Set[str] = set()

        if self.registry:
            all_agents = self.registry.list_all()
            for agent in all_agents:
                if not agent.auto_reload_on_skill_change:
                    continue

                # Check if this skill is bound to the agent
                if skill_name and skill_name in agent.bound_skills:
                    affected_agent_ids.add(agent.agent_id)

                # Check if agent watches this department
                dept_from_path = parts[-3] if len(parts) >= 3 else None
                if dept_from_path and dept_from_path in agent.auto_bind_departments:
                    affected_agent_ids.add(agent.agent_id)

        if affected_agent_ids:
            for agent_id in affected_agent_ids:
                self.reload_bus.publish(AgentReloadEvent(
                    agent_id=agent_id,
                    changed_file=file_path,
                    skill_name=skill_name,
                ))

                # Log reload event in registry
                if self.registry:
                    self.registry.record_reload(agent_id, file_path, success=True)

            logger.info(f"Reload published for agents: {affected_agent_ids}")
        else:
            # Broadcast to all agents
            self.reload_bus.publish(AgentReloadEvent(
                agent_id="*",
                changed_file=file_path,
                skill_name=skill_name,
            ))

        # ── Notify external agents with webhook_url ──────────────
        if self.registry:
            self._notify_external_agents(file_path, skill_name)

    def _notify_external_agents(self, file_path: str, skill_name: Optional[str]):
        """
        Send skill reload notifications to external agents that have
        a webhook_url configured and auto_reload_on_skill_change=True.
        Uses HMAC-signed headers for authenticated delivery.
        """
        if not self.registry:
            return

        import time as _time
        try:
            from middleware.webhook_security import build_authenticated_headers
            import requests
        except ImportError:
            logger.warning("webhook_security not available, skipping external notifications")
            return

        all_agents = self.registry.list_all()
        for agent in all_agents:
            if not getattr(agent, 'webhook_url', None):
                continue
            if not agent.auto_reload_on_skill_change:
                continue

            payload = {
                "event": "skill_reload",
                "skill_name": skill_name,
                "changed_file": os.path.basename(file_path),
                "timestamp": _time.time(),
            }
            headers = build_authenticated_headers(
                agent_auth_token=getattr(agent, 'auth_token', None),
                payload=payload,
            )

            try:
                resp = requests.post(
                    agent.webhook_url,
                    json=payload,
                    headers=headers,
                    timeout=5,
                )
                logger.info(
                    f"External reload notification to '{agent.agent_id}' "
                    f"at {agent.webhook_url}: {resp.status_code}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to notify external agent '{agent.agent_id}' "
                    f"at {agent.webhook_url}: {e}"
                )
