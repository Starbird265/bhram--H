"""
Layer 4: DISTILLATION
AI-powered extraction, noise filtering, type classification, and confidence scoring.

This is the core intelligence layer. It takes raw chunked text and:
  1. Filters out remaining noise that survived Layer 2
  2. Extracts operational signals (facts, SOPs, policies, edge cases, etc.)
  3. Classifies each signal into department + knowledge type
  4. Scores confidence and rewrites content into imperative guidelines
  5. Processes chunks in BATCHES (not one-by-one) for efficiency

When AI providers (Claude/Ollama) are not available, falls back to a
rule-based mock distiller that uses keyword matching for classification.
"""

import os
import uuid
from typing import List, Optional, Any
from datetime import datetime, timezone
from pydantic import BaseModel

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from core.models import (
    KnowledgeChunk, KnowledgeType, SourceType, Department,
    KnowledgeMetadata, ProcessingLayer
)

# Provider router for AI calls (Claude + Ollama + rule-based)
try:
    from providers.router import ProviderRouter
    from providers import AIRequest, ProviderName
    _ROUTER_AVAILABLE = True
except ImportError:
    _ROUTER_AVAILABLE = False

# Phase 3: hash cache + model router integration
try:
    from providers.hash_cache import HashCache
    from providers.model_router import route as _router_route, TaskType, ProviderChoice
    _PHASE3_AVAILABLE = True
except ImportError:
    _PHASE3_AVAILABLE = False


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
    suggested_rules: List[str] = []  # Keywords the AI suggests for future rule-based matching


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
- tags should be lowercase, no spaces, relevant keywords

## SELF-TRAINING (CRITICAL):
In addition to extracting signals, you MUST also return a `suggested_rules` list.
This list should contain short keyword phrases (2-4 words each) that would help a
keyword-based system identify similar operational signals in the future WITHOUT AI.
For example, if you extract a rule about deploying after 6pm, suggest: ["deploy after", "deployment time", "6pm deploy"].
Think: what would a simple keyword matcher need to look for to catch this rule next time?"""


class KnowledgeDistiller:
    """
    Layer 4 engine that transforms raw text chunks into classified,
    scored operational knowledge signals.

    Self-training loop:
      - Rule-based engine tries FIRST (zero cost)
      - If rules miss, AI processes the batch
      - AI returns signals + suggested_rules
      - suggested_rules are saved to DynamicRuleManager
      - Next time, the rule-based engine catches those patterns
      - Over time, AI calls decrease as the rule engine learns

    Phase 3 enhancements:
      - Hash cache: results are memoized; same content = 0 tokens on repeat
      - Model router: routes to cheapest available provider
    """

    BATCH_SIZE = 5  # Process this many chunks per AI call

    def __init__(self, router=None, cache: Optional[Any] = None, db_path: Optional[str] = None):
        # Use provided router or create one
        if router:
            self.router = router
        elif _ROUTER_AVAILABLE:
            self.router = ProviderRouter()
        else:
            self.router = None

        # Backward compat: client is truthy when AI is available
        if self.router:
            real_ai_available = any(
                p.get("available", False)
                for p in self.router.get_available_providers()
                if p.get("name") != "rule_based"
            )
            self.client = self.router if real_ai_available else None
        else:
            self.client = None

        # Phase 3: hash cache
        self._cache: Optional[HashCache] = None
        if cache is not None:
            self._cache = cache
        elif _PHASE3_AVAILABLE and db_path:
            self._cache = HashCache(db_path=db_path)

        # Self-training: dynamic rule manager
        self._rule_manager = None
        try:
            from pipeline.rule_manager import DynamicRuleManager
            _effective_db = db_path or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database"
            )
            self._rule_manager = DynamicRuleManager(db_path=_effective_db)
            dynamic_count = len(self._rule_manager.get_keywords())
            if dynamic_count > 0:
                print(f"  [Layer 4] Loaded {dynamic_count} AI-learned rules")
        except Exception as e:
            print(f"  [Layer 4] DynamicRuleManager unavailable: {e}")

        # Track cache stats for this session
        self._cache_hits = 0
        self._cache_misses = 0
        self._rules_first_hits = 0
        self._ai_fallback_calls = 0

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

        # Multi‑step refinement: run a second pass on low‑confidence signals
        refined = self.refine_chunks(all_distilled)
        return refined

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
        """Processes a batch: rules first, AI only if rules miss.

        Self-training flow:
          1. Try rule-based extraction (free, instant)
          2. If rules found signals → return them (AI never called)
          3. If rules found NOTHING → call AI
          4. AI returns signals + suggested_rules
          5. Save suggested_rules for next time
        """
        # Step 1: Always try rules first
        rule_results = self._mock_distill_batch(chunks)

        if rule_results:
            # Rules caught signals — no AI needed
            self._rules_first_hits += 1
            return rule_results

        # Step 2: Rules missed — fall back to AI (if available)
        if not self.client:
            return []  # No AI available either

        try:
            self._ai_fallback_calls += 1
            return self._ai_distill_batch(chunks)
        except Exception as e:
            print(f"[Layer 4] AI batch distillation failed: {e}.")
            return []

    def refine_chunks(self, chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
        """Second‑pass refinement for low‑confidence signals.
        Simple rule‑based booster: if confidence between 0.4‑0.6 and contains modal verbs, raise to 0.7.
        """
        refined = []
        for chunk in chunks:
            conf = chunk.metadata.confidence_score
            if 0.4 <= conf < 0.7:
                lowered = chunk.content.lower()
                if any(word in lowered for word in ["should", "must", "required", "need to"]):
                    chunk.metadata.confidence_score = 0.7
            refined.append(chunk)
        return refined

    def _ai_distill_batch(self, chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
        """Uses the provider router (Claude/Ollama) to distill a batch of chunks.

        Phase 3: checks hash cache before calling AI. Writes result to cache after.
        """
        import asyncio
        import json as json_mod

        # Build the batch input
        batch_text = ""
        for i, chunk in enumerate(chunks):
            batch_text += f"\n--- CHUNK {i + 1} (Source: {chunk.source_identifier}) ---\n"
            batch_text += chunk.content
            batch_text += "\n"

        # ── Phase 3: cache check ──────────────────────────────────────────────
        cache_key = None
        if self._cache is not None and _PHASE3_AVAILABLE:
            provider_name = self._pick_provider(chunks)
            cache_key = HashCache.make_key(
                prompt=batch_text,
                provider=provider_name,
                model="distill-v1",
            )
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache_hits += 1
                print(f"  [Layer 4] Cache HIT for batch of {len(chunks)} chunks — 0 tokens spent")
                try:
                    batch_result = DistillationBatch(**cached)
                    return self._signals_to_chunks(batch_result.signals, chunks)
                except Exception:
                    pass  # Cache corrupt — fall through to AI
        self._cache_misses += 1
        # ─────────────────────────────────────────────────────────────────────

        request = AIRequest(
            purpose="distill",
            messages=[
                {"role": "system", "content": DISTILLATION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Process these {len(chunks)} text chunks and extract all operational signals. Return valid JSON with a 'signals' array:\n{batch_text}"}
            ],
            response_schema=DistillationBatch,
            temperature=0.3,
        )

        # Run async router \u2014 safe in both CLI (no loop) and FastAPI (loop already running)
        try:
            response = asyncio.run(self.router.complete(request))
        except RuntimeError:
            # Already inside a running event loop (FastAPI context).
            # Use nest_asyncio if available, otherwise fall back to rule-based.
            try:
                import nest_asyncio as _nest
                _nest.apply()
                loop = asyncio.get_event_loop()
                response = loop.run_until_complete(self.router.complete(request))
            except ImportError:
                # nest_asyncio not installed \u2014 route to rule-based to avoid crash
                print("[Layer 4] nest_asyncio not installed; falling back to rule-based. "
                      "Run: pip install nest_asyncio")
                return self._mock_distill_batch(chunks)

        if response.error:
            print(f"[Layer 4] AI distillation error: {response.error}. Falling back to mock.")
            return self._mock_distill_batch(chunks)

        # Parse the response
        extracted_chunks = []
        signals = []
        suggested_rules = []

        if response.parsed and hasattr(response.parsed, 'signals'):
            signals = response.parsed.signals
            suggested_rules = getattr(response.parsed, 'suggested_rules', []) or []
        elif response.content:
            # Try to parse JSON from raw content
            try:
                data = json_mod.loads(response.content)
                if isinstance(data, dict) and 'signals' in data:
                    signals = [DistilledSignal(**s) for s in data['signals']]
                    suggested_rules = data.get('suggested_rules', []) or []
            except Exception:
                pass

        if not signals:
            return []

        extracted_chunks = self._signals_to_chunks(signals, chunks)

        # ── Self-training: save AI-suggested rules ─────────────────────────────
        if self._rule_manager and suggested_rules:
            added = self._rule_manager.add_rules(suggested_rules, source="ai_distill")
            if added > 0:
                print(f"  [Layer 4] 🧠 AI taught {added} new rules to the rule engine")

        # Also extract keywords from signal tags and titles as bonus rules
        if self._rule_manager and signals:
            bonus_keywords = []
            for sig in signals:
                if not sig.is_noise:
                    bonus_keywords.extend(sig.tags)
                    # Extract 2-word phrases from titles
                    words = sig.title.lower().split()
                    for i in range(len(words) - 1):
                        bonus_keywords.append(f"{words[i]} {words[i+1]}")
            if bonus_keywords:
                self._rule_manager.add_rules(bonus_keywords, source="ai_tags")

        # ── Phase 3: store result in cache ─────────────────────────────────────
        if self._cache is not None and cache_key and signals:
            try:
                self._cache.set(cache_key, {
                    "signals": [s.model_dump() for s in signals],
                    "suggested_rules": suggested_rules,
                })
            except Exception as e:
                print(f"  [Layer 4] Cache write failed (non-fatal): {e}")
        # ────────────────────────────────────────────────────────────────────

        return extracted_chunks

    def _signals_to_chunks(
        self, signals: List[DistilledSignal], source_chunks: List[KnowledgeChunk]
    ) -> List[KnowledgeChunk]:
        """Convert DistilledSignal list into KnowledgeChunk list.
        Used by both the live AI path and the cache-hit path.
        """
        extracted_chunks = []
        for signal in signals:
            if signal.is_noise:
                continue

            # Find the best matching source chunk for provenance
            source_chunk = source_chunks[0]
            for chunk in source_chunks:
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

    def _pick_provider(self, chunks: List[KnowledgeChunk]) -> str:
        """Use model_router to determine which provider to use for these chunks."""
        if not _PHASE3_AVAILABLE:
            return "ollama"
        total_length = sum(len(c.content) for c in chunks)
        # Check if any chunk has restricted sensitivity
        sensitivity = None
        for chunk in chunks:
            if hasattr(chunk, "metadata") and hasattr(chunk.metadata, "sensitivity_level"):
                lvl = getattr(chunk.metadata, "sensitivity_level", None)
                if lvl and hasattr(lvl, "value"):
                    sensitivity = lvl.value
                    break
        choice = _router_route(
            task=TaskType.DISTILL,
            content_length=total_length,
            sensitivity=sensitivity,
            ollama_available=True,   # Router has its own availability check
            claude_available=bool(os.getenv("ANTHROPIC_API_KEY")),
        )
        return choice.value

    def cache_stats(self) -> dict:
        """Return cache hit/miss stats for this distiller session."""
        total = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total * 100) if total > 0 else 0.0
        rule_stats = self._rule_manager.get_stats() if self._rule_manager else {}
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate_percent": round(hit_rate, 1),
            "tokens_saved_estimate": self._cache_hits * 500,
            "rules_first_hits": self._rules_first_hits,
            "ai_fallback_calls": self._ai_fallback_calls,
            "dynamic_rules": rule_stats,
        }

    # ─── Mock Fallback (Rule-Based) ──────────────────────────────

    def _mock_distill_batch(self, chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
        """
        Rule-based fallback when AI providers are not available.
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
        Splits compound content into individual rules and also handles "and" conjunctions.
        
        Filtering pipeline per line:
          1. Skip headings, short lines, title-like lines
          2. Reject non-operational content (blog posts, personal stories)
          3. Classify department + type + confidence
          4. Skip if confidence < 0.4
          5. Deduplicate within this chunk's signals
        """
        lines = chunk.content.split("\n")
        signals = []
        seen_normalized = set()

        # Patterns that indicate personal/blog content (not operational knowledge)
        BLOG_PATTERNS = [
            "i don't have", "i have a", "i'm from", "my college", "my degree",
            "my story", "my journey", "my experience", "growing up",
            "pricing plan", "₹", "$299", "$499", "$799", "₹299", "₹499", "₹799",
            "day 1", "day 10", "days 10", "days 15", "month 2",
            "reply rate", "revenue", "time spent vs",
            "what converted", "what to repeat",
            "everyone says", "what they don't say",
            "loom walkthrough", "bundle +",
            "fake screenshots", "no exaggeration",
            "found 5 critical", "found 3 critical",
            "tier 3 india", "tier 2 india",
            "building real things", "shipping real products",
            "serious automation is",
            "websites don't block code",
        ]

        for line in lines:
            stripped = line.strip()

            # Skip markdown headings — they are structure, not knowledge.
            if stripped.startswith("#"):
                continue

            stripped = stripped.lstrip("-•* ")
            if len(stripped.split()) < 4:
                continue  # Skip trivially short lines

            # Skip lines that look like section titles
            words = stripped.split()
            if (len(words) <= 6 and
                    all(w[0].isupper() or not w[0].isalpha() for w in words if w) and
                    not any(c in stripped for c in ".,:;-") and
                    not any(w.lower() in ("do", "must", "should", "never", "always",
                                          "use", "avoid", "set", "run", "call", "check")
                            for w in words)):
                continue

            # ── NEW: Reject blog/personal content ─────────────────────
            lower_stripped = stripped.lower()
            is_blog = any(pattern in lower_stripped for pattern in BLOG_PATTERNS)
            if is_blog:
                continue

            # ── Require operational signal keywords (static + AI-learned) ─
            OPERATIONAL_SIGNALS = [
                "always", "never", "must", "do not", "don't", "ensure", "make sure",
                "step", "deploy", "production", "staging", "database", "schema",
                "migration", "security", "approval", "review", "merge", "test",
                "ci", "cd", "pipeline", "rollback", "hotfix", "incident",
                "budget", "deadline", "compliance", "audit", "policy", "rule",
                "process", "workflow", "escalate", "sign-off", "branch", "tag",
                "release", "qa", "verify", "avoid", "important", "critical",
                "required", "should", "recommend", "permission", "access",
                "encrypt", "monitor", "alert", "on-call", "before", "after",
                "cap", "spend", "logo", "brand", "campaign",
            ]
            # Append AI-learned dynamic rules
            if self._rule_manager:
                OPERATIONAL_SIGNALS.extend(self._rule_manager.get_keywords())

            has_signal = any(kw in lower_stripped for kw in OPERATIONAL_SIGNALS)
            if not has_signal:
                continue  # Not operational content — skip entirely

            # Split on conjunctions that likely contain multiple rules
            sub_parts = [part.strip() for part in stripped.split(' and ') if part.strip()]
            for part in sub_parts:
                # Classify the signal
                dept = self._classify_department(part)
                k_type = self._classify_type(part)
                confidence = self._score_confidence(part)
                title = self._generate_title(part)

                if confidence < 0.4:
                    continue  # Raised from 0.3 — skip uncertain content

                # ── NEW: Deduplicate within this chunk ────────────────
                import re as _re
                norm = _re.sub(r'[^\w\s]', '', part.lower().strip())
                norm = _re.sub(r'\s+', ' ', norm)
                if norm in seen_normalized:
                    continue
                # Also check substring containment
                is_dup = any(norm in s or s in norm for s in seen_normalized if len(s) > 15)
                if is_dup:
                    continue
                seen_normalized.add(norm)

                signal = KnowledgeChunk(
                    id=str(uuid.uuid4()),
                    department=dept,
                    knowledge_type=k_type,
                    source_type=chunk.source_type,
                    source_identifier=chunk.source_identifier,
                    title=title,
                    content=part,
                    summary=part[:120] + ("..." if len(part) > 120 else ""),
                    tags=self._extract_tags(part),
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
        """Generate a concise, searchable title from the content."""
        # Use up to first 6 significant words to keep titles short and focused
        words = text.split()
        skip_words = {"also", "and", "but", "so", "then", "note", "important", "warning"}
        significant = [w for w in words if w.lower().strip(",:;.") not in skip_words]
        title_words = significant[:6]
        title = " ".join(title_words)
        # Truncate excessively long titles
        if len(title) > 55:
            title = title[:52] + "..."
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

        # Extract hashtag‑style tags (e.g., #devops)
        import re
        hashtags = re.findall(r'#([a-z0-9_-]+)', lower)
        tags.extend(hashtags)

        # Deduplicate while preserving order and cap to 10 tags
        seen = set()
        deduped = []
        for t in tags:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
            if len(deduped) >= 10:
                break
        return deduped
