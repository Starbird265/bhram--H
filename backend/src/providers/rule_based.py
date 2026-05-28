"""
Rule-Based Provider Adapter.

Deterministic, zero-latency, zero-cost processing for operations
that don't need AI. Wraps the existing keyword-matching logic
from the distiller and classifier behind the provider interface.

This is the guaranteed-available fallback — it always works offline.
"""

import time
from typing import List, Optional, Any

from providers import (
    AIProvider, AIRequest, AIResponse, EmbedResponse,
    ProviderName,
)


class RuleBasedAdapter:
    """Deterministic rule-based adapter — the always-available fallback."""

    @property
    def name(self) -> ProviderName:
        return ProviderName.RULE_BASED

    @property
    def is_online(self) -> bool:
        return False

    @property
    def is_available(self) -> bool:
        return True  # Always available

    @property
    def supports_structured_output(self) -> bool:
        return False  # Returns raw text only

    @property
    def max_context_tokens(self) -> int:
        return 999_999  # No real limit for local processing

    async def complete(self, request: AIRequest) -> AIResponse:
        """Rule-based processing — returns a simple acknowledgment.

        The actual rule-based logic lives in the pipeline modules
        (distiller, classifier, deduplicator). This adapter exists
        so the router can log that rule-based processing was chosen.
        """
        start = time.time()

        # Extract the user message content
        user_content = ""
        for msg in request.messages:
            if msg["role"] == "user":
                user_content += msg["content"] + "\n"

        return AIResponse(
            provider=self.name,
            model="rule-based-v1",
            content=f"[RULE_BASED] Processed {len(user_content)} chars for purpose: {request.purpose}",
            parsed=None,
            input_tokens=len(user_content.split()),
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=(time.time() - start) * 1000,
        )

    async def embed(self, texts: List[str]) -> EmbedResponse:
        """Rule-based can't generate embeddings."""
        return EmbedResponse(
            provider=self.name, model="rule-based-v1",
            embeddings=[], dimensions=0,
        )

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0
