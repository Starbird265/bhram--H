"""
Layer 11: Agent Context Assembler

Builds per-agent system prompts using 3-layer progressive disclosure:
  Layer A — Always loaded: identity + skill stubs + tool stubs (~200-500 tokens)
  Layer B — On demand:     Full SKILL.md body (loaded when skill is relevant)
  Layer C — On request:    CONTEXT.md, failure patterns, glossary
"""

import os
import re
from pathlib import Path
from typing import List, Optional, Dict

from core.models import AgentProfile


class AgentContextAssembler:
    """
    Builds a fully resolved, token-budgeted system prompt for a
    given AgentProfile at session start.

    Uses progressive disclosure: agents start with a slim identity
    header + skill stubs, and load full skill/context files on demand.
    """

    def __init__(self, knowledge_base_dir: str):
        """
        Args:
            knowledge_base_dir: Root path to the knowledge_base/ directory
                                where SKILL.md and CONTEXT.md files live.
        """
        self.kb_dir = knowledge_base_dir

    def assemble(self, agent: AgentProfile) -> str:
        """
        Build the complete system prompt for an agent.

        Returns concatenated markdown of:
          - Identity header
          - Available skill summaries
          - Available tool summaries
          - Full skill content (for all bound skills)
          - Context files
        All within the agent's max_context_tokens budget.
        """
        parts = []

        # ── LAYER A: Identity Header (always loaded) ─────────────
        parts.append(self._build_identity_header(agent))

        # ── LAYER A: Skill metadata stubs ────────────────────────
        skill_stubs = self._load_skill_stubs(agent)
        if skill_stubs:
            parts.append(skill_stubs)

        # ── LAYER A: Tool stubs ──────────────────────────────────
        tool_stubs = self._build_tool_stubs(agent)
        if tool_stubs:
            parts.append(tool_stubs)

        # ── LAYER B: Full skill content ──────────────────────────
        skill_bodies = self._load_full_skills(agent)
        if skill_bodies:
            parts.append(skill_bodies)

        # ── LAYER C: Context files ───────────────────────────────
        context_content = self._load_context_files(agent)
        if context_content:
            parts.append(context_content)

        # ── Enforce token budget ─────────────────────────────────
        full_prompt = "\n\n---\n\n".join(parts)
        return self._enforce_token_limit(full_prompt, agent.max_context_tokens)

    def assemble_layer_a_only(self, agent: AgentProfile) -> str:
        """Build only the lightweight Layer A context (identity + stubs)."""
        parts = [
            self._build_identity_header(agent),
            self._load_skill_stubs(agent),
            self._build_tool_stubs(agent),
        ]
        return "\n\n".join(p for p in parts if p)

    # ── Layer A Builders ──────────────────────────────────────────

    def _build_identity_header(self, agent: AgentProfile) -> str:
        return f"""# You are {agent.display_name}

**Role**: {agent.role.value}
**Department**: {agent.department}
**Mission**: {agent.description}

You operate within the BHRM Organizational Intelligence Engine. You follow only
approved, canonical knowledge from your assigned SKILL.md files. You do not
speculate beyond your knowledge base. You escalate contested or ambiguous
situations to a human reviewer."""

    def _load_skill_stubs(self, agent: AgentProfile) -> str:
        """Load only name + description from each bound skill's SKILL.md frontmatter."""
        if not agent.bound_skills:
            return "## Available Skills\n\n_No skills bound to this agent yet._"

        stubs = ["## Available Skills\n"]
        for skill_name in agent.bound_skills:
            # Try to find the SKILL.md file
            meta = self._parse_skill_frontmatter(skill_name)
            if meta:
                stubs.append(f"- **{meta.get('name', skill_name)}**: {meta.get('description', 'No description')}")
            else:
                stubs.append(f"- **{skill_name}**: _(skill file not found)_")
        return "\n".join(stubs)

    def _build_tool_stubs(self, agent: AgentProfile) -> str:
        """Build one-line description of available tools."""
        if not agent.tools_allowlist:
            return ""

        stubs = ["## Available Tools\n"]
        for tool_name in agent.tools_allowlist:
            if tool_name in agent.tools_denylist:
                continue  # Deny wins
            stubs.append(f"- `{tool_name}`")
        return "\n".join(stubs)

    # ── Layer B: Full Skill Content ───────────────────────────────

    def _load_full_skills(self, agent: AgentProfile) -> str:
        """Load complete SKILL.md body for all bound skills."""
        if not agent.bound_skills:
            return ""

        sections = ["## Operational Knowledge\n"]
        for skill_name in agent.bound_skills:
            content = self._read_skill_file(skill_name)
            if content:
                sections.append(f"### {skill_name}\n\n{content}")
            else:
                sections.append(f"### {skill_name}\n\n_⚠ Skill file not found. Run pipeline to generate._")
        return "\n\n".join(sections)

    # ── Layer C: Context Files ────────────────────────────────────

    def _load_context_files(self, agent: AgentProfile) -> str:
        """Load CONTEXT.md files for agent's department and 'shared'."""
        context_parts = []

        # Department-specific context
        dept_context = self._read_context_file(agent.department)
        if dept_context:
            context_parts.append(f"## {agent.department.title()} Department Context\n\n{dept_context}")

        # Shared context (always loaded for all agents)
        if agent.department != "shared":
            shared_context = self._read_context_file("shared")
            if shared_context:
                context_parts.append(f"## Shared Context\n\n{shared_context}")

        # Explicit context_files from the profile
        for ctx_path in agent.context_files:
            if os.path.isfile(ctx_path):
                try:
                    text = Path(ctx_path).read_text(encoding="utf-8")
                    context_parts.append(f"## Context: {os.path.basename(ctx_path)}\n\n{text}")
                except Exception:
                    pass

        return "\n\n".join(context_parts) if context_parts else ""

    # ── File I/O Helpers ──────────────────────────────────────────

    def _find_skill_path(self, skill_name: str) -> Optional[str]:
        """Find the SKILL.md file for a given skill name across all department dirs."""
        if not os.path.isdir(self.kb_dir):
            return None

        for dept_dir in Path(self.kb_dir).iterdir():
            if dept_dir.is_dir():
                skill_dir = dept_dir / skill_name
                skill_file = skill_dir / "SKILL.md"
                if skill_file.is_file():
                    return str(skill_file)

                # Also check flat structure: {dept}/{skill_name}.md
                flat_file = dept_dir / f"{skill_name}" / "SKILL.md"
                if flat_file.is_file():
                    return str(flat_file)

        return None

    def _read_skill_file(self, skill_name: str) -> Optional[str]:
        """Read the full SKILL.md content for a skill."""
        path = self._find_skill_path(skill_name)
        if not path:
            return None
        try:
            return Path(path).read_text(encoding="utf-8")
        except Exception:
            return None

    def _parse_skill_frontmatter(self, skill_name: str) -> Optional[Dict[str, str]]:
        """Extract YAML frontmatter (name, description) from SKILL.md."""
        content = self._read_skill_file(skill_name)
        if not content:
            return None

        # Parse YAML frontmatter between --- markers
        match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if not match:
            return {"name": skill_name, "description": content[:120]}

        frontmatter = {}
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                frontmatter[key.strip()] = val.strip().strip('"').strip("'")
        return frontmatter

    def _read_context_file(self, department: str) -> Optional[str]:
        """Read CONTEXT.md for a department."""
        if not os.path.isdir(self.kb_dir):
            return None

        context_path = os.path.join(self.kb_dir, department, "CONTEXT.md")
        if os.path.isfile(context_path):
            try:
                return Path(context_path).read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    # ── Token Budget ──────────────────────────────────────────────

    def _enforce_token_limit(self, text: str, max_tokens: int) -> str:
        """
        Truncate text to stay within token budget.
        Uses a rough 1 token ≈ 4 chars estimate (conservative for English).
        """
        max_chars = max_tokens * 4
        if len(text) <= max_chars:
            return text

        truncated = text[:max_chars]
        # Try to cut at last paragraph break
        last_break = truncated.rfind("\n\n")
        if last_break > max_chars * 0.7:
            truncated = truncated[:last_break]

        return truncated + "\n\n---\n_[Context truncated to fit token budget]_"
