"""
Layer 5: DEDUPLICATION
Similarity-based merge/update/discard against the canonical knowledge store.

Key improvements over naive dedup:
  1. Pre-filters by department + knowledge_type to reduce comparison space
  2. Uses title + content similarity heuristics before calling AI
  3. Sends ONLY the top-K most similar chunks to AI (not the entire DB)
  4. Handles UPDATE by preserving the original chunk ID for in-place replacement
  5. Tracks merge reasoning for audit trails
"""

import os
import re
from typing import List, Tuple, Optional
from difflib import SequenceMatcher
from pydantic import BaseModel

from core.models import KnowledgeChunk, ProcessingLayer

try:
    from providers.router import ProviderRouter
    from providers import AIRequest
    _ROUTER_AVAILABLE = True
except ImportError:
    _ROUTER_AVAILABLE = False


# ─── AI Response Schema ─────────────────────────────────────────

class DeduplicationDecision(BaseModel):
    action: str  # "ADD_NEW", "UPDATE_EXISTING", "DISCARD_REDUNDANT"
    existing_chunk_id: Optional[str] = None
    reasoning: str


# ─── Configuration ───────────────────────────────────────────────

MAX_CANDIDATES_FOR_AI = 5     # Max similar chunks to send to AI
SIMILARITY_THRESHOLD = 0.35   # Minimum similarity to consider as candidate
EXACT_MATCH_THRESHOLD = 0.80  # Above this = auto-discard without AI (lowered from 0.9)


# ─── The Deduplication Engine ────────────────────────────────────

class KnowledgeDeduplicator:
    """
    Layer 5 engine that evaluates new chunks against the canonical store
    and decides whether to ADD, UPDATE, or DISCARD each chunk.
    """

    def __init__(self, router=None):
        if router:
            self.router = router
        elif _ROUTER_AVAILABLE:
            self.router = ProviderRouter()
        else:
            self.router = None
        self.client = self.router

    @staticmethod
    def _normalize_for_hash(text: str) -> str:
        """Normalize text for fast exact-hash comparison."""
        text = text.lower().strip()
        text = re.sub(r'[^\w\s]', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text

    def evaluate_chunk(
        self,
        new_chunk: KnowledgeChunk,
        existing_chunks: List[KnowledgeChunk]
    ) -> Tuple[str, Optional[KnowledgeChunk]]:
        """
        Evaluates a new chunk against existing chunks.
        Returns: (action, chunk_to_save)
          action: "ADD" | "UPDATE" | "DISCARD"
          chunk_to_save: The chunk to persist (may have updated ID for UPDATE)
        """
        if not existing_chunks:
            new_chunk.processing_layer = ProcessingLayer.DEDUPLICATED
            return "ADD", new_chunk

        # Step 0 (FAST): Normalized text hash check — catches exact & near-exact duplicates
        new_norm = self._normalize_for_hash(new_chunk.content)
        for existing in existing_chunks:
            existing_norm = self._normalize_for_hash(existing.content)
            # Exact normalized match
            if new_norm == existing_norm:
                return "DISCARD", None
            # Substring containment: one chunk fully contains the other
            if len(new_norm) > 20 and len(existing_norm) > 20:
                if new_norm in existing_norm:
                    return "DISCARD", None
                if existing_norm in new_norm:
                    # New chunk is more detailed — treat as UPDATE
                    new_chunk.id = existing.id
                    new_chunk.processing_layer = ProcessingLayer.DEDUPLICATED
                    return "UPDATE", new_chunk

        # Step 1: Find similar candidates (pre-filter)
        candidates = self._find_similar_candidates(new_chunk, existing_chunks)

        if not candidates:
            new_chunk.processing_layer = ProcessingLayer.DEDUPLICATED
            return "ADD", new_chunk

        # Step 2: Check for near-exact matches (auto-discard)
        best_score, best_match = candidates[0]
        if best_score >= EXACT_MATCH_THRESHOLD:
            return "DISCARD", None

        # Step 3: Use AI for nuanced decision (or mock fallback)
        if self.client:
            try:
                return self._ai_evaluate(new_chunk, candidates)
            except Exception as e:
                print(f"[Layer 5] AI dedup failed: {e}. Falling back to heuristic.")

        return self._heuristic_evaluate(new_chunk, candidates)

    def evaluate_batch(
        self,
        new_chunks: List[KnowledgeChunk],
        existing_chunks: List[KnowledgeChunk]
    ) -> List[Tuple[str, Optional[KnowledgeChunk]]]:
        """Evaluate a batch of chunks. Updates existing_chunks as we go to avoid self-duplication."""
        results = []
        # Build a growing list so new ADDs are checked against each other too
        all_existing = list(existing_chunks)

        for chunk in new_chunks:
            action, processed = self.evaluate_chunk(chunk, all_existing)
            results.append((action, processed))
            if action == "ADD" and processed:
                all_existing.append(processed)

        return results

    # ─── Similarity Engine ───────────────────────────────────────

    def _find_similar_candidates(
        self,
        new_chunk: KnowledgeChunk,
        existing_chunks: List[KnowledgeChunk]
    ) -> List[Tuple[float, KnowledgeChunk]]:
        """
        Finds the most similar existing chunks using a multi-signal scoring approach.
        Returns list of (score, chunk) sorted by similarity descending.
        """
        scored = []

        for existing in existing_chunks:
            score = self._compute_similarity(new_chunk, existing)
            if score >= SIMILARITY_THRESHOLD:
                scored.append((score, existing))

        # Sort by similarity, take top K
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:MAX_CANDIDATES_FOR_AI]

    @staticmethod
    def _word_jaccard(text_a: str, text_b: str) -> float:
        """Word-level Jaccard similarity coefficient."""
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / len(words_a | words_b)

    @staticmethod
    def _compute_similarity(chunk_a: KnowledgeChunk, chunk_b: KnowledgeChunk) -> float:
        """
        Multi-signal similarity score combining:
          - Title similarity (20%)
          - Content SequenceMatcher (30%)
          - Content word Jaccard (20%)
          - Tag overlap (10%)
          - Same department + type bonus (10%)
          - Normalized hash containment bonus (10%)
        """
        # Title similarity
        title_sim = SequenceMatcher(
            None,
            chunk_a.title.lower(),
            chunk_b.title.lower()
        ).ratio()

        # Content similarity (compare first 2000 chars for accuracy vs speed balance)
        content_a = chunk_a.content.lower()[:2000]
        content_b = chunk_b.content.lower()[:2000]
        content_sim = SequenceMatcher(None, content_a, content_b).ratio()

        # Word-level Jaccard for catching semantic overlap
        jaccard_sim = KnowledgeDeduplicator._word_jaccard(content_a, content_b)

        # Tag overlap
        tags_a = set(chunk_a.tags)
        tags_b = set(chunk_b.tags)
        tag_sim = len(tags_a & tags_b) / max(len(tags_a | tags_b), 1)

        # Department + type match bonus
        dept_type_bonus = 0.0
        if chunk_a.department == chunk_b.department:
            dept_type_bonus += 0.5
        if chunk_a.knowledge_type == chunk_b.knowledge_type:
            dept_type_bonus += 0.5

        # Normalized containment bonus
        norm_a = re.sub(r'[^\w\s]', '', content_a)
        norm_b = re.sub(r'[^\w\s]', '', content_b)
        containment_bonus = 1.0 if (norm_a in norm_b or norm_b in norm_a) else 0.0

        return (
            title_sim * 0.20 +
            content_sim * 0.30 +
            jaccard_sim * 0.20 +
            tag_sim * 0.10 +
            dept_type_bonus * 0.10 +
            containment_bonus * 0.10
        )

    # ─── AI-Powered Decision ─────────────────────────────────────

    def _ai_evaluate(
        self,
        new_chunk: KnowledgeChunk,
        candidates: List[Tuple[float, KnowledgeChunk]]
    ) -> Tuple[str, Optional[KnowledgeChunk]]:
        """Uses AI to make nuanced ADD/UPDATE/DISCARD decisions."""
        # Build a bounded context string with ONLY the top candidates
        existing_str = ""
        for score, c in candidates:
            existing_str += f"ID: {c.id} | Title: {c.title} | Similarity: {score:.2f}\n"
            existing_str += f"Content: {c.content[:300]}...\n\n"

        prompt = f"""Evaluate the NEW CHUNK against the MOST SIMILAR EXISTING CHUNKS.

EXISTING CHUNKS (most similar first):
{existing_str}

NEW CHUNK:
Title: {new_chunk.title}
Content: {new_chunk.content}

Decide:
- ADD_NEW: Entirely new information not covered by any existing chunk.
- UPDATE_EXISTING: Contains newer/better information that should replace an existing chunk. Provide existing_chunk_id.
- DISCARD_REDUNDANT: Already fully covered by an existing chunk."""

        import asyncio
        import json as json_mod

        request = AIRequest(
            purpose="distill",
            messages=[{"role": "user", "content": prompt}],
            response_schema=DeduplicationDecision,
            temperature=0.2,
        )

        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(self.router.complete(request))
        finally:
            loop.close()

        if response.error:
            return self._heuristic_evaluate(new_chunk, candidates)

        decision = None
        if response.parsed and hasattr(response.parsed, 'action'):
            decision = response.parsed
        elif response.content:
            try:
                data = json_mod.loads(response.content)
                decision = DeduplicationDecision(**data)
            except Exception:
                pass

        if not decision:
            return self._heuristic_evaluate(new_chunk, candidates)

        if decision.action == "ADD_NEW":
            new_chunk.processing_layer = ProcessingLayer.DEDUPLICATED
            return "ADD", new_chunk
        elif decision.action == "UPDATE_EXISTING" and decision.existing_chunk_id:
            new_chunk.id = decision.existing_chunk_id
            new_chunk.processing_layer = ProcessingLayer.DEDUPLICATED
            return "UPDATE", new_chunk
        else:
            return "DISCARD", None

    # ─── Heuristic Fallback ──────────────────────────────────────

    def _heuristic_evaluate(
        self,
        new_chunk: KnowledgeChunk,
        candidates: List[Tuple[float, KnowledgeChunk]]
    ) -> Tuple[str, Optional[KnowledgeChunk]]:
        """
        Rule-based fallback when AI is not available.
        Uses similarity score thresholds for decisions.
        """
        best_score, best_match = candidates[0]

        # Very similar content — check if new chunk has more info
        if best_score >= 0.6:  # Lowered from 0.7 to catch more near-duplicates
            # If new chunk is longer, treat as UPDATE (more info)
            if len(new_chunk.content) > len(best_match.content) * 1.2:
                new_chunk.id = best_match.id
                new_chunk.processing_layer = ProcessingLayer.DEDUPLICATED
                return "UPDATE", new_chunk
            # Otherwise, it's redundant
            return "DISCARD", None

        # Also check word Jaccard — catches paraphrased duplicates
        jaccard = self._word_jaccard(new_chunk.content, best_match.content)
        if jaccard >= 0.6:
            return "DISCARD", None

        # Moderately similar — different enough to add
        new_chunk.processing_layer = ProcessingLayer.DEDUPLICATED
        return "ADD", new_chunk
