"""
Smart Layer: LLM Classifier
Classifies raw text into structured KnowledgeChunks using AI or rule-based fallback.
Used for ad-hoc classification outside the main pipeline (e.g., webhook single-message processing).
"""

import os
import uuid
from typing import List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel

from core.models import (
    KnowledgeType, Department, KnowledgeChunk,
    KnowledgeMetadata, SourceType, ProcessingLayer
)

try:
    from providers.router import ProviderRouter
    from providers import AIRequest
    _ROUTER_AVAILABLE = True
except ImportError:
    _ROUTER_AVAILABLE = False


class ClassificationResult(BaseModel):
    department: Department
    knowledge_type: KnowledgeType
    tags: List[str]
    summary: str
    is_noise: bool


class LLMClassifier:
    """Classifies raw text into structured KnowledgeChunks."""

    def __init__(self, router=None):
        if router:
            self.router = router
        elif _ROUTER_AVAILABLE:
            self.router = ProviderRouter()
        else:
            self.router = None
        self.client = self.router

    def classify_chunk(self, raw_text: str, source: str) -> Optional[KnowledgeChunk]:
        """Classifies a raw string into a structured KnowledgeChunk using an LLM."""
        if not self.client:
            return self._mock_classify(raw_text, source)

        try:
            import asyncio
            import json as json_mod

            request = AIRequest(
                purpose="classify",
                messages=[
                    {"role": "system", "content": "You are an expert organizational knowledge classifier. Analyze the text and classify it into the correct department and knowledge type. If the text is casual chat or irrelevant noise, set is_noise to true."},
                    {"role": "user", "content": f"Source: {source}\n\nText: {raw_text}"}
                ],
                response_schema=ClassificationResult,
                temperature=0.3,
            )

            loop = asyncio.new_event_loop()
            try:
                response = loop.run_until_complete(self.router.complete(request))
            finally:
                loop.close()

            if response.error:
                return self._mock_classify(raw_text, source)

            result = None
            if response.parsed and hasattr(response.parsed, 'is_noise'):
                result = response.parsed
            elif response.content:
                try:
                    data = json_mod.loads(response.content)
                    result = ClassificationResult(**data)
                except Exception:
                    pass

            if not result or result.is_noise:
                return None

            return KnowledgeChunk(
                id=str(uuid.uuid4()),
                department=result.department,
                knowledge_type=result.knowledge_type,
                source_type=SourceType.SLACK,
                source_identifier=source,
                title=raw_text[:60].strip().rstrip(".,;:"),
                content=raw_text,
                summary=result.summary,
                tags=result.tags,
                metadata=KnowledgeMetadata(
                    confidence_score=0.8,
                    source_reliability=0.8,
                    source_refs=[source]
                ),
                processing_layer=ProcessingLayer.DISTILLED,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
        except Exception as e:
            print(f"LLM Classification failed: {e}. Falling back to mock.")
            return self._mock_classify(raw_text, source)

    def _mock_classify(self, raw_text: str, source: str) -> Optional[KnowledgeChunk]:
        """Rule-based classification fallback."""
        dept = Department.SHARED
        k_type = KnowledgeType.GLOSSARY

        lower_text = raw_text.lower()
        if "marketing" in lower_text or "ad" in lower_text or "brand" in lower_text:
            dept = Department.MARKETING
        elif "code" in lower_text or "deploy" in lower_text or "pull request" in lower_text or "engineering" in lower_text:
            dept = Department.ENGINEERING

        if "never" in lower_text or "do not" in lower_text:
            k_type = KnowledgeType.FAILURE_PATTERN
        elif "step" in lower_text or "process" in lower_text or "must" in lower_text:
            k_type = KnowledgeType.SOP

        return KnowledgeChunk(
            id=str(uuid.uuid4()),
            department=dept,
            knowledge_type=k_type,
            source_type=SourceType.SLACK,
            source_identifier=source,
            title=raw_text[:60].strip().rstrip(".,;:"),
            content=raw_text,
            summary=raw_text[:120] + ("..." if len(raw_text) > 120 else ""),
            tags=["auto-classified"],
            metadata=KnowledgeMetadata(
                confidence_score=0.6,
                source_reliability=0.8,
                source_refs=[source]
            ),
            processing_layer=ProcessingLayer.DISTILLED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
