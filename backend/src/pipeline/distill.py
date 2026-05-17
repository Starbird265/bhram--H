"""
Layer 4: DISTILLATION
AI-powered extraction, noise filtering, type classification, and confidence scoring.

This is the core intelligence layer. It takes raw chunked text and:
  1. Filters out remaining noise that survived Layer 2
  2. Extracts operational signals (facts, SOPs, policies, edge cases, etc.)
  3. Classifies each signal into department + knowledge type
  4. Scores confidence and rewrites content into imperative guidelines
  5. Processes chunks in BATCHES (not one-by-one) for efficiency

When OpenAI API is not available, falls back to a rule-based mock distiller
that uses keyword matching for classification.
"""

import os
import uuid
from typing import List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel

from core.models import (
    KnowledgeChunk, KnowledgeType, SourceType, Department,
    KnowledgeMetadata, ProcessingLayer
)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# ─── AI Response Schemas ─────────────────────────────────────────

class DistilledSignal(BaseModel):
    """A single operational signal extracted by the AI."""
    title: str
    knowledge_type: KnowledgeType
    department: Department
    summary: str
    cleaned_content: str
    tags: List[str]
    is_noise: bool
    confidence_score: float
    source_reliability: float


class DistillationBatch(BaseModel):
    """Response from AI for a batch of chunks."""
    signals: List[DistilledSignal]


# ─── The Distillation Engine ─────────────────────────────────────

DISTILLATION_SYSTEM_PROMPT = """You are an Operational Intelligence Distillation Engine for an organizational brain system.

Your job is to process text chunks from company communication channels and documents, and extract ONLY actionable operational knowledge.

## STRICT RULES:
1. FILTER OUT all conversational noise — pleasantries, casual chat, lunch plans, emoji reactions, "sounds good", "thanks", etc.
2. EXTRACT actionable signals: facts, SOPs, policies, security rules, edge cases, tool workflows, approval flows, escalation paths, preferences, glossary terms, customer context.
3. REWRITE each signal as a clear, imperative guideline — not conversational text.
4. CLASSIFY each signal into the correct KnowledgeType and Department.
5. SCORE confidence (0.0-1.0) based on how definitive the statement is:
   - 1.0 = Explicit rule ("NEVER deploy after 6pm")
   - 0.7-0.9 = Strong recommendation ("You should always tag #devops")
   - 0.4-0.6 = Implicit preference ("We usually do X")
   - 0.0-0.3 = Uncertain or speculative
6. If a chunk is ENTIRELY noise with zero operational signals, return an empty signals list.
7. SEPARATE compound rules into individual signals. "Don't deploy after 6pm and always tag #devops" = 2 separate signals.
8. Use the title field to create a descriptive, searchable name (NOT "Mock Distilled Rule").

## OUTPUT QUALITY:
- cleaned_content should be the rule rewritten in imperative form
- summary should be 1 sentence explaining the rule
- tags should be lowercase, no spaces, relevant keywords"""


class KnowledgeDistiller:
    """
    Layer 4 engine that transforms raw text chunks into classified,
    scored operational knowledge signals.
    """

    BATCH_SIZE = 5  # Process this many chunks per AI call

    def __init__(self):
        self.client = OpenAI() if OpenAI and os.getenv("OPENAI_API_KEY") else None

    def distill_chunks(self, chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
        """
        Main entrypoint: Takes Layer 3 chunks and produces Layer 4 distilled chunks.
        Processes in batches for efficiency.
        """
        all_distilled = []

        # Process in batches
        for i in range(0, len(chunks), self.BATCH_SIZE):
            batch = chunks[i:i + self.BATCH_SIZE]
            distilled = self._process_batch(batch)
            all_distilled.extend(distilled)

        return all_distilled

    def distill_text(
        self,
        text: str,
        source_type: SourceType,
        source_identifier: str,
        department: Department = Department.SHARED
    ) -> List[KnowledgeChunk]:
        """
        Convenience method: Distills raw text directly (creates a temporary chunk).
        Used by webhook ingestion for single-document processing.
        """
        temp_chunk = KnowledgeChunk(
            id=str(uuid.uuid4()),
            department=department,
            knowledge_type=KnowledgeType.SOP,
            source_type=source_type,
            source_identifier=source_identifier,
            title="Raw Input",
            content=text,
            summary="",
            processing_layer=ProcessingLayer.CHUNKED,
        )
        return self.distill_chunks([temp_chunk])

    # ─── Core Processing ─────────────────────────────────────────

    def _process_batch(self, chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
        """Processes a batch of chunks through AI or mock fallback."""
        if not self.client:
            return self._mock_distill_batch(chunks)

        try:
            return self._ai_distill_batch(chunks)
        except Exception as e:
            print(f"[Layer 4] AI batch distillation failed: {e}. Falling back to mock.")
            return self._mock_distill_batch(chunks)

    def _ai_distill_batch(self, chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
        """Uses OpenAI structured outputs to distill a batch of chunks in one API call."""
        # Build the batch input
        batch_text = ""
        for i, chunk in enumerate(chunks):
            batch_text += f"\n--- CHUNK {i + 1} (Source: {chunk.source_identifier}) ---\n"
            batch_text += chunk.content
            batch_text += "\n"

        response = self.client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": DISTILLATION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Process these {len(chunks)} text chunks and extract all operational signals:\n{batch_text}"}
            ],
            response_format=DistillationBatch,
        )

        extracted_chunks = []
        for signal in response.choices[0].message.parsed.signals:
            if signal.is_noise:
                continue

            # Find the best matching source chunk for provenance
            source_chunk = chunks[0]  # Default to first
            for chunk in chunks:
                if any(word in chunk.content.lower() for word in signal.title.lower().split()[:3]):
                    source_chunk = chunk
                    break

            distilled = KnowledgeChunk(
                id=str(uuid.uuid4()),
                department=signal.department,
                knowledge_type=signal.knowledge_type,
                source_type=source_chunk.source_type,
                source_identifier=source_chunk.source_identifier,
                title=signal.title,
                content=signal.cleaned_content,
                summary=signal.summary,
                tags=signal.tags,
                metadata=KnowledgeMetadata(
                    confidence_score=signal.confidence_score,
                    source_reliability=signal.source_reliability,
                    verification_count=0,
                    last_confirmed_at=datetime.now(timezone.utc),
                    source_refs=[source_chunk.source_identifier],
                    source_position=source_chunk.metadata.source_position
                ),
                processing_layer=ProcessingLayer.DISTILLED,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            extracted_chunks.append(distilled)

        return extracted_chunks

    # ─── Mock Fallback (Rule-Based) ──────────────────────────────

    def _mock_distill_batch(self, chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
        """
        Rule-based fallback when OpenAI is not available.
        Uses keyword matching to classify and extract operational signals.
        """
        results = []

        for chunk in chunks:
            signals = self._extract_signals_from_text(chunk)
            results.extend(signals)

        return results

    def _extract_signals_from_text(self, chunk: KnowledgeChunk) -> List[KnowledgeChunk]:
        """
        Extracts individual signals from a chunk using keyword analysis.
        Splits compound content into individual rules.
        """
        lines = chunk.content.split("\n")
        signals = []

        for line in lines:
            stripped = line.strip().lstrip("-•* ")
            if len(stripped.split()) < 4:
                continue  # Skip trivially short lines

            # Classify the signal
            dept = self._classify_department(stripped)
            k_type = self._classify_type(stripped)
            confidence = self._score_confidence(stripped)
            title = self._generate_title(stripped)

            if confidence < 0.3:
                continue  # Skip low-confidence noise

            signal = KnowledgeChunk(
                id=str(uuid.uuid4()),
                department=dept,
                knowledge_type=k_type,
                source_type=chunk.source_type,
                source_identifier=chunk.source_identifier,
                title=title,
                content=stripped,
                summary=stripped[:120] + ("..." if len(stripped) > 120 else ""),
                tags=self._extract_tags(stripped),
                metadata=KnowledgeMetadata(
                    confidence_score=confidence,
                    source_reliability=chunk.metadata.source_reliability,
                    verification_count=0,
                    last_confirmed_at=datetime.now(timezone.utc),
                    source_refs=[chunk.source_identifier],
                    source_position=chunk.metadata.source_position
                ),
                processing_layer=ProcessingLayer.DISTILLED,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            signals.append(signal)

        return signals

    @staticmethod
    def _classify_department(text: str) -> Department:
        """Classify department based on content keywords."""
        lower = text.lower()
        dept_keywords = {
            Department.ENGINEERING: [
                "deploy", "code", "pull request", "merge", "branch", "ci", "cd",
                "database", "schema", "migration", "api", "server", "docker",
                "terraform", "infrastructure", "staging", "production", "hotfix",
                "rollback", "github", "git", "test", "debug", "engineer"
            ],
            Department.MARKETING: [
                "campaign", "brand", "logo", "ad", "advertis", "content",
                "seo", "social media", "utm", "analytics", "copywriting",
                "creative", "audience", "marketing", "launch", "webinar"
            ],
            Department.SALES: [
                "deal", "prospect", "pipeline", "quota", "commission", "crm",
                "demo", "pricing", "discount", "contract", "renewal", "sales",
                "customer", "account", "lead", "opportunity"
            ],
            Department.OPS: [
                "onboarding", "offboarding", "compliance", "audit", "vendor",
                "budget", "procurement", "facility", "hr", "policy", "training",
                "expense", "operations"
            ],
        }

        scores = {dept: 0 for dept in Department}
        for dept, keywords in dept_keywords.items():
            for kw in keywords:
                if kw in lower:
                    scores[dept] += 1

        best_dept = max(scores, key=scores.get)
        return best_dept if scores[best_dept] > 0 else Department.SHARED

    @staticmethod
    def _classify_type(text: str) -> KnowledgeType:
        """Classify knowledge type based on content patterns."""
        lower = text.lower()

        type_patterns = {
            KnowledgeType.SECURITY_RULE: ["security", "vulnerab", "auth", "permission", "access control", "encrypt"],
            KnowledgeType.FAILURE_PATTERN: ["never", "do not", "don't", "avoid", "fail", "broke", "incident", "outage", "crash"],
            KnowledgeType.EDGE_CASE: ["edge case", "exception", "unless", "however", "but if", "special case", "corner case"],
            KnowledgeType.POLICY: ["policy", "compliance", "regulation", "must comply", "gdpr", "legal"],
            KnowledgeType.APPROVAL_FLOW: ["approval", "approve", "sign off", "sign-off", "authorize"],
            KnowledgeType.ESCALATION: ["escalate", "escalation", "notify", "alert", "page", "on-call"],
            KnowledgeType.TOOL_WORKFLOW: ["terraform", "docker", "vercel", "github", "jira", "linear", "notion"],
            KnowledgeType.SOP: ["step", "process", "first", "then", "ensure", "make sure", "always", "must", "before", "after"],
            KnowledgeType.DECISION: ["decided", "decision", "chose", "selected", "going with", "switched to"],
            KnowledgeType.GLOSSARY: ["means", "defined as", "refers to", "aka", "known as"],
            KnowledgeType.PREFERENCE: ["prefer", "preference", "usually", "we like", "our standard"],
        }

        for k_type, patterns in type_patterns.items():
            if any(p in lower for p in patterns):
                return k_type

        return KnowledgeType.SOP  # Default

    @staticmethod
    def _score_confidence(text: str) -> float:
        """Score confidence based on definitiveness of the statement."""
        lower = text.lower()

        # High confidence indicators
        if any(w in lower for w in ["never", "always", "must", "do not", "don't", "critical", "mandatory"]):
            return 0.95
        if any(w in lower for w in ["important", "ensure", "make sure", "required", "rule"]):
            return 0.85
        if any(w in lower for w in ["should", "recommend", "best practice", "ideally"]):
            return 0.7
        if any(w in lower for w in ["usually", "typically", "often", "we tend to"]):
            return 0.5
        if any(w in lower for w in ["maybe", "might", "possibly", "could"]):
            return 0.3

        return 0.6  # Default moderate confidence

    @staticmethod
    def _generate_title(text: str) -> str:
        """Generate a descriptive title from the content."""
        # Take first 8 significant words
        words = text.split()
        # Remove common prefixes
        skip_words = {"also", "and", "but", "so", "then", "note:", "important:", "warning:"}
        significant = [w for w in words if w.lower().rstrip(",:;") not in skip_words]
        title_words = significant[:8]
        title = " ".join(title_words)
        if len(title) > 60:
            title = title[:57] + "..."
        return title.rstrip(".,;:")

    @staticmethod
    def _extract_tags(text: str) -> List[str]:
        """Extract relevant tags from content."""
        lower = text.lower()
        tags = []

        tag_keywords = {
            "deployment": ["deploy", "release", "rollout"],
            "security": ["security", "auth", "permission", "encrypt"],
            "database": ["database", "schema", "migration", "sql"],
            "api": ["api", "endpoint", "rest", "graphql"],
            "ci-cd": ["ci", "cd", "pipeline", "github-actions"],
            "infrastructure": ["terraform", "docker", "kubernetes", "k8s", "aws"],
            "testing": ["test", "qa", "quality"],
            "monitoring": ["monitor", "alert", "logging", "observability"],
            "compliance": ["compliance", "audit", "gdpr", "regulation"],
            "budget": ["budget", "cost", "spend", "pricing"],
        }

        for tag, keywords in tag_keywords.items():
            if any(kw in lower for kw in keywords):
                tags.append(tag)

        # Extract hashtag-style tags
        import re
        hashtags = re.findall(r'#([a-z0-9_-]+)', lower)
        tags.extend(hashtags[:5])

        return list(set(tags))[:10]  # Dedupe and cap at 10
