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

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


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

    def __init__(self):
        self.client = OpenAI() if OpenAI and os.getenv("OPENAI_API_KEY") else None

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
        """Uses AI to synthesize chunks into a coherent skill document."""
        chunks_text = ""
        for i, chunk in enumerate(chunks):
            chunks_text += f"\n[Chunk {i+1}] Type: {chunk.knowledge_type.value} | Confidence: {chunk.metadata.confidence_score}\n"
            chunks_text += f"Title: {chunk.title}\n"
            chunks_text += f"Content: {chunk.content}\n"

        response = self.client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Skill Name: {skill_name}\nDepartment: {department.value}\nDescription: {description}\n\nKnowledge Chunks:\n{chunks_text}"}
            ],
            response_format=SynthesizedSkill,
        )

        result = response.choices[0].message.parsed

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

    # ─── Rule-Based Assembly ─────────────────────────────────────

    def _rule_based_assemble(
        self,
        skill_name: str,
        description: str,
        chunks: List[KnowledgeChunk],
        department: Department
    ) -> SkillDef:
        """Categorizes chunks by knowledge type and builds structured skill sections."""
        prerequisites = []
        steps = []
        edge_cases = []
        examples = []

        for chunk in chunks:
            content = f"**{chunk.title}**: {chunk.content}"

            if chunk.knowledge_type == KnowledgeType.SOP:
                steps.append(content)
            elif chunk.knowledge_type in [KnowledgeType.EDGE_CASE, KnowledgeType.FAILURE_PATTERN]:
                edge_cases.append(content)
            elif chunk.knowledge_type in [KnowledgeType.POLICY, KnowledgeType.SECURITY_RULE, KnowledgeType.GLOSSARY]:
                prerequisites.append(content)
            elif chunk.knowledge_type == KnowledgeType.DECISION:
                prerequisites.append(f"[DECISION] {content}")
            elif chunk.knowledge_type == KnowledgeType.TOOL_WORKFLOW:
                steps.append(f"[TOOL] {content}")
            elif chunk.knowledge_type == KnowledgeType.APPROVAL_FLOW:
                steps.append(f"[APPROVAL REQUIRED] {content}")
            elif chunk.knowledge_type == KnowledgeType.ESCALATION:
                edge_cases.append(f"[ESCALATION PATH] {content}")
            elif chunk.knowledge_type == KnowledgeType.CUSTOMER_CONTEXT:
                prerequisites.append(f"[CLIENT CONTEXT] {content}")
            elif chunk.knowledge_type == KnowledgeType.PREFERENCE:
                edge_cases.append(f"[PREFERENCE] {content}")

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
