"""
Layer 6: SYNTHESIS — Skill Assembly
Assembles KnowledgeChunks into coherent, agent-consumable SkillDef objects.

Key improvements:
  - Confidence-gated assembly (only chunks >= 0.7 confidence)
  - Intelligent categorization by knowledge type
  - AI-powered content synthesis for coherent output (when available)
  - Proper department tagging on the SkillDef
"""

import os
from typing import List, Optional
from pydantic import BaseModel

from core.models import KnowledgeChunk, SkillDef, Department, KnowledgeType

# Provider router for AI calls (Claude + Ollama + rule-based)
try:
    from providers.router import ProviderRouter
    from providers import AIRequest
    _ROUTER_AVAILABLE = True
except ImportError:
    _ROUTER_AVAILABLE = False


class SynthesizedSkill(BaseModel):
    """AI response for skill synthesis."""
    overview: str
    prerequisites: List[str]
    steps: List[str]
    examples: List[dict]
    edge_cases: List[str]


SYNTHESIS_SYSTEM_PROMPT = """You are an AI Skill Document Synthesizer. You receive a set of organizational knowledge chunks and must synthesize them into a coherent, well-structured skill document.

## OUTPUT RULES:
1. overview: 2-3 sentences describing what this skill covers and when an agent should use it.
2. prerequisites: List of things that must be true before following the steps (access, tools, permissions).
3. steps: Ordered list of imperative instructions. Merge related chunks into logical steps. Be specific and actionable.
4. examples: 1-2 realistic input/output examples showing how an agent would use this skill.
   Each example should be a dict with "input" and "output" keys.
5. edge_cases: List of warnings, "what NOT to do", failure patterns, and special cases.

## QUALITY RULES:
- Rewrite everything in imperative form ("Do X", "Ensure Y", "Never Z")
- Remove redundancy between chunks
- Organize steps in logical execution order
- Keep each item concise (1-2 sentences max)
"""


class SkillAssembler:
    """
    Assembles filtered KnowledgeChunks into a SkillDef.
    Uses AI for synthesis when available, falls back to rule-based assembly.
    """

    def __init__(self, router=None):
        if router:
            self.router = router
        elif _ROUTER_AVAILABLE:
            self.router = ProviderRouter()
        else:
            self.router = None
        # Backward compat
        self.client = self.router

    def assemble_skill(
        self,
        skill_name: str,
        description: str,
        chunks: List[KnowledgeChunk],
        department: Department
    ) -> SkillDef:
        """
        Main entrypoint: Assembles a SkillDef from relevant chunks.
        Filters by confidence, categorizes by type, then synthesizes.
        """
        # Gate: Only include chunks with sufficient confidence
        qualified_chunks = [
            c for c in chunks
            if c.metadata.confidence_score >= 0.7
        ]

        if not qualified_chunks:
            qualified_chunks = chunks[:5]  # Fallback: use top 5 even if low confidence

        # Try AI synthesis first
        if self.client and len(qualified_chunks) > 0:
            try:
                return self._ai_assemble(skill_name, description, qualified_chunks, department)
            except Exception as e:
                print(f"[Layer 6] AI synthesis failed: {e}. Falling back to rule-based.")

        return self._rule_based_assemble(skill_name, description, qualified_chunks, department)

    # ─── AI-Powered Synthesis ─────────────────────────────────────

    def _ai_assemble(
        self,
        skill_name: str,
        description: str,
        chunks: List[KnowledgeChunk],
        department: Department
    ) -> SkillDef:
        """Uses the provider router (Claude/Ollama) to synthesize chunks into a skill."""
        import asyncio
        import json as json_mod

        chunks_text = ""
        for i, chunk in enumerate(chunks):
            chunks_text += f"\n[Chunk {i+1}] Type: {chunk.knowledge_type.value} | Confidence: {chunk.metadata.confidence_score}\n"
            chunks_text += f"Title: {chunk.title}\n"
            chunks_text += f"Content: {chunk.content}\n"

        request = AIRequest(
            purpose="synthesize",
            messages=[
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Skill Name: {skill_name}\nDepartment: {department.value}\nDescription: {description}\n\nKnowledge Chunks:\n{chunks_text}"}
            ],
            response_schema=SynthesizedSkill,
            temperature=0.3,
        )

        try:
            response = asyncio.run(self.router.complete(request))
        except RuntimeError:
            try:
                import nest_asyncio
                nest_asyncio.apply()
                loop = asyncio.get_event_loop()
                response = loop.run_until_complete(self.router.complete(request))
            except ImportError:
                return self._rule_based_assemble(skill_name, description, chunks, department)

        if response.error:
            print(f"[Layer 6] AI synthesis error: {response.error}. Falling back to rule-based.")
            return self._rule_based_assemble(skill_name, description, chunks, department)

        # Parse the response
        result = None
        if response.parsed and hasattr(response.parsed, 'overview'):
            result = response.parsed
        elif response.content:
            try:
                data = json_mod.loads(response.content)
                result = SynthesizedSkill(**data)
            except Exception:
                pass

        if not result:
            return self._rule_based_assemble(skill_name, description, chunks, department)

        return SkillDef(
            name=skill_name,
            description=description,
            department=department,
            overview=result.overview,
            prerequisites=result.prerequisites,
            steps=result.steps,
            examples=result.examples,
            edge_cases=result.edge_cases,
            source_chunk_ids=[c.id for c in chunks]
        )

    # ─── Dedup & Filtering Helpers ───────────────────────────────

    @staticmethod
    def _normalize_for_dedup(text: str) -> str:
        """Normalize text for dedup comparison: lowercase, strip, collapse whitespace, remove punctuation."""
        import re
        text = text.lower().strip()
        text = re.sub(r'[^\w\s]', '', text)       # remove punctuation
        text = re.sub(r'\s+', ' ', text)           # collapse whitespace
        return text

    @staticmethod
    def _is_operational_content(text: str) -> bool:
        """Check if text contains operational signals (not just blog/personal content)."""
        operational_keywords = [
            "always", "never", "must", "do not", "don't", "ensure", "make sure",
            "step", "deploy", "production", "staging", "database", "schema",
            "migration", "security", "approval", "review", "merge", "test",
            "ci", "cd", "pipeline", "rollback", "hotfix", "incident",
            "budget", "deadline", "compliance", "audit", "policy", "rule",
            "process", "workflow", "checklist", "escalate", "sign-off",
            "tag", "release", "branch", "pr", "qa", "verify",
        ]
        lower = text.lower()
        return any(kw in lower for kw in operational_keywords)

    @staticmethod
    def _extract_clean_lines(content: str) -> list:
        """Split content into individual clean lines, filtering blanks."""
        lines = []
        for line in content.split('\n'):
            stripped = line.strip().lstrip('-•*0123456789. ')
            if stripped and len(stripped) > 10:
                lines.append(stripped)
        return lines

    def _dedup_list(self, items: list, max_items: int = 20) -> list:
        """Deduplicate a list of strings by normalized form. Keep first occurrence."""
        seen = set()
        result = []
        for item in items:
            norm = self._normalize_for_dedup(item)
            # Also check if this is a substring of something already seen
            if norm in seen:
                continue
            is_substring = any(norm in s or s in norm for s in seen if len(s) > 15)
            if is_substring:
                continue
            seen.add(norm)
            result.append(item)
            if len(result) >= max_items:
                break
        return result

    # ─── Rule-Based Assembly ─────────────────────────────────────

    def _rule_based_assemble(
        self,
        skill_name: str,
        description: str,
        chunks: List[KnowledgeChunk],
        department: Department
    ) -> SkillDef:
        """Categorizes chunks by knowledge type and builds structured, deduplicated skill sections."""
        prerequisites = []
        steps = []
        edge_cases = []
        examples = []

        for chunk in chunks:
            # Extract individual lines from content (not title — title is often a truncated copy)
            lines = self._extract_clean_lines(chunk.content)
            if not lines:
                # Fallback: use content as a single entry
                lines = [chunk.content.strip()] if chunk.content.strip() else []

            for line in lines:
                # Skip non-operational content (blog posts, personal stories, etc.)
                if not self._is_operational_content(line):
                    continue

                # Truncate to first sentence if too long (>200 chars)
                if len(line) > 200:
                    # Cut at first sentence boundary
                    for sep in ['. ', '! ', '? ']:
                        idx = line.find(sep)
                        if 20 < idx < 200:
                            line = line[:idx + 1]
                            break
                    else:
                        line = line[:200].rsplit(' ', 1)[0] + '...'

                if chunk.knowledge_type == KnowledgeType.SOP:
                    steps.append(line)
                elif chunk.knowledge_type in [KnowledgeType.EDGE_CASE, KnowledgeType.FAILURE_PATTERN]:
                    edge_cases.append(line)
                elif chunk.knowledge_type in [KnowledgeType.POLICY, KnowledgeType.SECURITY_RULE, KnowledgeType.GLOSSARY]:
                    prerequisites.append(line)
                elif chunk.knowledge_type == KnowledgeType.DECISION:
                    prerequisites.append(line)
                elif chunk.knowledge_type == KnowledgeType.TOOL_WORKFLOW:
                    steps.append(line)
                elif chunk.knowledge_type == KnowledgeType.APPROVAL_FLOW:
                    steps.append(line)
                elif chunk.knowledge_type == KnowledgeType.ESCALATION:
                    edge_cases.append(line)
                elif chunk.knowledge_type == KnowledgeType.CUSTOMER_CONTEXT:
                    prerequisites.append(line)
                elif chunk.knowledge_type == KnowledgeType.PREFERENCE:
                    edge_cases.append(line)

        # Deduplicate each section
        prerequisites = self._dedup_list(prerequisites, max_items=10)
        steps = self._dedup_list(steps, max_items=20)
        edge_cases = self._dedup_list(edge_cases, max_items=15)

        return SkillDef(
            name=skill_name,
            description=description,
            department=department,
            overview=f"Automatically assembled skill based on {len(chunks)} knowledge chunks from the {department.value} department.",
            prerequisites=prerequisites,
            steps=steps,
            edge_cases=edge_cases,
            examples=examples,
            source_chunk_ids=[c.id for c in chunks]
        )
