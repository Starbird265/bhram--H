"""
Claude Provider Adapter.

Wraps the Anthropic Messages API for tool-use reasoning,
long-context synthesis, and review tasks.
"""

import os
import time
import json
from typing import List, Optional, Any

from providers import (
    AIProvider, AIRequest, AIResponse, EmbedResponse,
    ProviderName,
)

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False


class ClaudeAdapter:
    """Anthropic Claude adapter implementing the AIProvider protocol."""

    DEFAULT_MODEL = "claude-sonnet-4-20250514"

    # Approximate pricing per 1M tokens (USD)
    _PRICING = {
        "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
        "claude-haiku-4-20250514": {"input": 0.80, "output": 4.00},
    }

    def __init__(self, model: Optional[str] = None):
        self._model = model or os.getenv("CLAUDE_MODEL", self.DEFAULT_MODEL)
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=api_key) if _ANTHROPIC_AVAILABLE and api_key else None

    @property
    def name(self) -> ProviderName:
        return ProviderName.CLAUDE

    @property
    def is_online(self) -> bool:
        return True

    @property
    def is_available(self) -> bool:
        return self._client is not None

    @property
    def supports_structured_output(self) -> bool:
        return True  # Via tool_use with JSON schema

    @property
    def max_context_tokens(self) -> int:
        return 200_000

    async def complete(self, request: AIRequest) -> AIResponse:
        """Send a completion request to Claude."""
        if not self._client:
            return AIResponse(
                provider=self.name, model=self._model,
                error="Claude client not available (missing API key or anthropic package)"
            )

        start = time.time()
        try:
            # Separate system message from user messages
            system_content = ""
            messages = []
            for msg in request.messages:
                if msg["role"] == "system":
                    system_content += msg["content"] + "\n"
                else:
                    messages.append(msg)

            # If structured output requested, use tool_use pattern
            kwargs = {
                "model": self._model,
                "max_tokens": request.max_tokens,
                "messages": messages,
            }
            if system_content:
                kwargs["system"] = system_content.strip()

            if request.response_schema:
                # Use tool_use to get structured output
                schema = request.response_schema.model_json_schema()
                kwargs["tools"] = [{
                    "name": "structured_response",
                    "description": "Return the structured response",
                    "input_schema": schema,
                }]
                kwargs["tool_choice"] = {"type": "tool", "name": "structured_response"}

            response = self._client.messages.create(**kwargs)

            content = ""
            parsed = None

            for block in response.content:
                if block.type == "text":
                    content += block.text
                elif block.type == "tool_use":
                    try:
                        parsed = request.response_schema(**block.input)
                        content = json.dumps(block.input)
                    except Exception:
                        content = json.dumps(block.input)

            return AIResponse(
                provider=self.name, model=self._model,
                content=content, parsed=parsed,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=self.estimate_cost(
                    response.usage.input_tokens, response.usage.output_tokens
                ),
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return AIResponse(
                provider=self.name, model=self._model,
                error=str(e),
                latency_ms=(time.time() - start) * 1000,
            )

    async def embed(self, texts: List[str]) -> EmbedResponse:
        """Claude doesn't natively support embeddings — return empty."""
        return EmbedResponse(
            provider=self.name, model=self._model,
            embeddings=[], dimensions=0,
        )

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost in USD."""
        pricing = self._PRICING.get(self._model, {"input": 3.00, "output": 15.00})
        return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
