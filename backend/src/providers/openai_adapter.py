"""
OpenAI Provider Adapter.

Wraps the OpenAI API for structured extraction, synthesis,
and embedding. Supports gpt-4o-mini and text-embedding-3-small.
"""

import os
import time
from typing import List, Optional, Any

from providers import (
    AIProvider, AIRequest, AIResponse, EmbedResponse,
    ProviderName,
)

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False


class OpenAIAdapter:
    """OpenAI API adapter implementing the AIProvider protocol."""

    DEFAULT_CHAT_MODEL = "gpt-4o-mini"
    DEFAULT_EMBED_MODEL = "text-embedding-3-small"

    # Approximate pricing per 1M tokens (USD)
    _PRICING = {
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    }

    def __init__(self, model: Optional[str] = None):
        self._model = model or self.DEFAULT_CHAT_MODEL
        api_key = os.getenv("OPENAI_API_KEY")
        self._client = OpenAI() if _OPENAI_AVAILABLE and api_key else None

    @property
    def name(self) -> ProviderName:
        return ProviderName.OPENAI

    @property
    def is_online(self) -> bool:
        return True

    @property
    def is_available(self) -> bool:
        return self._client is not None

    @property
    def supports_structured_output(self) -> bool:
        return True

    @property
    def max_context_tokens(self) -> int:
        return 128_000 if "4o" in self._model else 16_384

    async def complete(self, request: AIRequest) -> AIResponse:
        """Send a completion request to OpenAI."""
        if not self._client:
            return AIResponse(
                provider=self.name, model=self._model,
                error="OpenAI client not available (missing API key or package)"
            )

        start = time.time()
        try:
            if request.response_schema:
                response = self._client.beta.chat.completions.parse(
                    model=self._model,
                    messages=request.messages,
                    response_format=request.response_schema,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                )
                parsed = response.choices[0].message.parsed
                content = response.choices[0].message.content
            else:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=request.messages,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                )
                parsed = None
                content = response.choices[0].message.content

            usage = response.usage
            input_tok = usage.prompt_tokens if usage else 0
            output_tok = usage.completion_tokens if usage else 0

            return AIResponse(
                provider=self.name, model=self._model,
                content=content, parsed=parsed,
                input_tokens=input_tok, output_tokens=output_tok,
                cost_usd=self.estimate_cost(input_tok, output_tok),
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return AIResponse(
                provider=self.name, model=self._model,
                error=str(e),
                latency_ms=(time.time() - start) * 1000,
            )

    async def embed(self, texts: List[str]) -> EmbedResponse:
        """Generate embeddings via OpenAI."""
        if not self._client:
            return EmbedResponse(
                provider=self.name, model=self.DEFAULT_EMBED_MODEL,
                embeddings=[], dimensions=0,
            )

        try:
            response = self._client.embeddings.create(
                model=self.DEFAULT_EMBED_MODEL,
                input=texts,
            )
            embeddings = [item.embedding for item in response.data]
            dims = len(embeddings[0]) if embeddings else 0
            return EmbedResponse(
                provider=self.name, model=self.DEFAULT_EMBED_MODEL,
                embeddings=embeddings, dimensions=dims,
                cost_usd=self.estimate_cost(sum(len(t.split()) for t in texts) * 2, 0),
            )
        except Exception:
            return EmbedResponse(
                provider=self.name, model=self.DEFAULT_EMBED_MODEL,
                embeddings=[], dimensions=0,
            )

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost in USD."""
        pricing = self._PRICING.get(self._model, {"input": 0.15, "output": 0.60})
        return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
