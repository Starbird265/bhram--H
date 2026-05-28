"""
AI Provider Protocol and shared types.

Every provider adapter implements this interface so the router
can swap between OpenAI, Claude, Ollama, and rule-based processing
without any calling code needing to know which is active.
"""

from typing import List, Optional, Any, Dict, Protocol, runtime_checkable
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime, timezone


class ProviderName(str, Enum):
    OPENAI = "openai"
    CLAUDE = "claude"
    OLLAMA = "ollama"
    RULE_BASED = "rule_based"
    GENERIC_LOCAL = "generic_local"


class AIRequest(BaseModel):
    """Standard request envelope for all AI operations."""
    purpose: str                        # "distill", "classify", "embed", "synthesize", etc.
    messages: List[Dict[str, str]]      # [{"role": "system", "content": ...}, ...]
    response_schema: Optional[Any] = None  # Pydantic model class for structured output
    max_tokens: int = 4096
    temperature: float = 0.3
    source_ids: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AIResponse(BaseModel):
    """Standard response envelope from all AI operations."""
    provider: ProviderName
    model: str
    content: Optional[str] = None       # Raw text response
    parsed: Optional[Any] = None        # Parsed structured response
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    error: Optional[str] = None


class EmbedResponse(BaseModel):
    """Response from embedding operations."""
    provider: ProviderName
    model: str
    embeddings: List[List[float]]
    dimensions: int = 0
    cost_usd: float = 0.0


class RoutingDecision(BaseModel):
    """Records why a specific provider was chosen."""
    provider: ProviderName
    model: str
    purpose: str
    input_sensitivity: str              # SensitivityLevel value
    redaction_applied: bool = False
    source_ids: List[str] = Field(default_factory=list)
    output_schema: Optional[str] = None
    cost_estimate_usd: float = 0.0
    fallback_chain: List[str] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str = ""


@runtime_checkable
class AIProvider(Protocol):
    """Interface every provider adapter must implement."""

    @property
    def name(self) -> ProviderName: ...

    @property
    def is_online(self) -> bool: ...

    @property
    def is_available(self) -> bool: ...

    @property
    def supports_structured_output(self) -> bool: ...

    @property
    def max_context_tokens(self) -> int: ...

    async def complete(self, request: AIRequest) -> AIResponse: ...

    async def embed(self, texts: List[str]) -> EmbedResponse: ...

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float: ...
