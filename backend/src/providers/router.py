"""
AI Provider Router — The central routing engine (fully dynamic).

Auto-detects all available providers at startup and re-checks periodically:
  - Ollama: auto-discovers models from running daemon
  - Claude: auto-detects ANTHROPIC_API_KEY
  - OpenAI: auto-detects OPENAI_API_KEY
  - Rule-based: always available (zero-cost fallback)

Decides which provider handles each request based on:
  - Data sensitivity level
  - Source privacy mode
  - Provider availability (live-checked, not hardcoded)
  - Task type and quality requirements
  - User preference (CORTEX_PREFERRED_PROVIDER env or API config)
  - Cost optimization

Every routing decision is logged for audit.
"""

import os
import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from core.models import SensitivityLevel, PrivacyMode
from providers import (
    AIProvider, AIRequest, AIResponse, EmbedResponse,
    ProviderName, RoutingDecision,
)
from providers.claude_adapter import ClaudeAdapter
from providers.ollama_adapter import OllamaAdapter
from providers.openai_adapter import OpenAIAdapter
from providers.rule_based import RuleBasedAdapter


# ─── Task quality tiers ──────────────────────────────────────────

HIGH_QUALITY_TASKS = {"synthesize", "review", "resolve_conflict", "generate_filter"}
STANDARD_TASKS = {"distill", "classify", "extract", "score"}
EMBED_TASKS = {"embed"}
RULE_TASKS = {"validate", "format", "deduplicate_exact"}


class ProviderRouter:
    """
    Routes AI requests to the best available provider.

    Fully dynamic — no hardcoded model names or API keys.
    Everything is auto-detected from the running environment.

    Initialization:
      1. Rule-based (always available)
      2. Ollama (auto-discovers models from daemon)
      3. OpenAI (if OPENAI_API_KEY is set)
      4. Claude (if ANTHROPIC_API_KEY is set)
    """

    def __init__(self):
        self._rule_based = RuleBasedAdapter()
        self._ollama = OllamaAdapter()
        self._claude = ClaudeAdapter()
        self._openai = OpenAIAdapter()

        # Provider registry in fallback order
        self._providers: Dict[ProviderName, Any] = {
            ProviderName.RULE_BASED: self._rule_based,
            ProviderName.OLLAMA: self._ollama,
            ProviderName.OPENAI: self._openai,
            ProviderName.CLAUDE: self._claude,
        }

        # User preference (checked from env or set via API)
        self._preferred: Optional[ProviderName] = self._detect_user_preference()

        # Decision log
        self._decisions: List[RoutingDecision] = []

    @staticmethod
    def _detect_user_preference() -> Optional[ProviderName]:
        """Check env for user's explicit provider preference."""
        pref = os.getenv("CORTEX_PREFERRED_PROVIDER", "").strip().lower()
        if not pref:
            return None
        _MAP = {
            "ollama": ProviderName.OLLAMA,
            "claude": ProviderName.CLAUDE,
            "openai": ProviderName.OPENAI,
        }
        return _MAP.get(pref)

    def set_preferred_provider(self, provider_name: Optional[str]):
        """Allow runtime override of preferred provider (from API/config)."""
        if not provider_name:
            self._preferred = None
            return
        _MAP = {
            "ollama": ProviderName.OLLAMA,
            "claude": ProviderName.CLAUDE,
            "openai": ProviderName.OPENAI,
        }
        self._preferred = _MAP.get(provider_name.lower())

    # ─── Public API ──────────────────────────────────────────────

    async def complete(
        self,
        request: AIRequest,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        privacy_mode: PrivacyMode = PrivacyMode.ONLINE_ALLOWED,
        preferred_provider: Optional[ProviderName] = None,
    ) -> AIResponse:
        """Route a completion request to the best provider."""
        # Use explicit preference > instance preference > None
        effective_preferred = preferred_provider or self._preferred

        chain = self._build_fallback_chain(
            request.purpose, sensitivity, privacy_mode, effective_preferred
        )

        decision = RoutingDecision(
            provider=chain[0] if chain else ProviderName.RULE_BASED,
            model="",
            purpose=request.purpose,
            input_sensitivity=sensitivity.value,
            source_ids=request.source_ids,
            fallback_chain=[p.value for p in chain],
            reason=f"Sensitivity={sensitivity.value}, Privacy={privacy_mode.value}",
        )

        # Try each provider in the chain
        for provider_name in chain:
            provider = self._providers.get(provider_name)
            if not provider or not provider.is_available:
                continue

            decision.provider = provider_name
            decision.model = getattr(provider, '_model', getattr(provider, '_chat_model', 'unknown'))

            response = await provider.complete(request)

            if response.error is None:
                decision.cost_estimate_usd = response.cost_usd
                self._decisions.append(decision)
                return response

        # All providers failed — rule-based
        decision.provider = ProviderName.RULE_BASED
        decision.reason += " | All providers failed, using rule-based fallback"
        self._decisions.append(decision)
        return await self._rule_based.complete(request)

    async def embed(
        self,
        texts: List[str],
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        privacy_mode: PrivacyMode = PrivacyMode.ONLINE_ALLOWED,
    ) -> EmbedResponse:
        """Route an embedding request to the best provider."""
        if privacy_mode == PrivacyMode.HARD_LOCAL or sensitivity == SensitivityLevel.RESTRICTED:
            if self._ollama.is_available:
                return await self._ollama.embed(texts)
            return EmbedResponse(
                provider=ProviderName.RULE_BASED, model="none",
                embeddings=[], dimensions=0,
            )

        # Prefer Ollama for embeddings (free, local)
        if self._ollama.is_available:
            result = await self._ollama.embed(texts)
            if result.embeddings:
                return result

        # Try OpenAI embeddings (if available)
        if self._openai.is_available:
            result = await self._openai.embed(texts)
            if result.embeddings:
                return result

        return EmbedResponse(
            provider=ProviderName.RULE_BASED, model="none",
            embeddings=[], dimensions=0,
        )

    def get_available_providers(self) -> List[Dict[str, Any]]:
        """Return live status of all providers — fully dynamic."""
        result = []
        for name, provider in self._providers.items():
            info = {
                "name": name.value,
                "available": provider.is_available,
                "online": provider.is_online,
                "structured_output": provider.supports_structured_output,
            }
            # Add dynamic model info
            if name == ProviderName.OLLAMA and hasattr(provider, '_chat_model'):
                info["model"] = provider._chat_model
                info["cost"] = "free"
                info["base_url"] = provider._base_url
                if hasattr(provider, 'get_discovered_models'):
                    info["discovered_models"] = provider.get_discovered_models()
            elif name == ProviderName.CLAUDE and hasattr(provider, '_model'):
                info["model"] = provider._model
                info["cost"] = "paid"
            elif name == ProviderName.OPENAI and hasattr(provider, '_model'):
                info["model"] = provider._model
                info["cost"] = "paid"
            elif name == ProviderName.RULE_BASED:
                info["model"] = "rule-based-v1"
                info["cost"] = "free"

            result.append(info)

        return result

    def get_recent_decisions(self, limit: int = 20) -> List[RoutingDecision]:
        return self._decisions[-limit:]

    def clear_decisions(self) -> List[RoutingDecision]:
        decisions = self._decisions[:]
        self._decisions = []
        return decisions

    # ─── Routing Logic ───────────────────────────────────────────

    def _build_fallback_chain(
        self,
        purpose: str,
        sensitivity: SensitivityLevel,
        privacy_mode: PrivacyMode,
        preferred: Optional[ProviderName] = None,
    ) -> List[ProviderName]:
        """Build an ordered list of providers to try.

        Rules:
          - RESTRICTED data → rule-based only
          - HARD_LOCAL privacy → Ollama + rule-based only
          - User preference → tried first (if available)
          - HIGH_QUALITY tasks → best available first
          - STANDARD tasks → cheapest available first
          - Always ends with RULE_BASED as ultimate fallback
        """
        chain: List[ProviderName] = []

        # Hard restrictions
        if sensitivity == SensitivityLevel.RESTRICTED:
            return [ProviderName.RULE_BASED]

        if privacy_mode == PrivacyMode.HARD_LOCAL:
            return [ProviderName.OLLAMA, ProviderName.RULE_BASED]

        # User preference
        if preferred and preferred not in (ProviderName.RULE_BASED,):
            chain.append(preferred)

        # Task-based routing
        if purpose in RULE_TASKS:
            return [ProviderName.RULE_BASED]

        if purpose in HIGH_QUALITY_TASKS:
            # Quality: Claude > OpenAI > Ollama
            if sensitivity in (SensitivityLevel.PUBLIC, SensitivityLevel.INTERNAL):
                for p in (ProviderName.CLAUDE, ProviderName.OPENAI):
                    if p not in chain:
                        chain.append(p)
            if ProviderName.OLLAMA not in chain:
                chain.append(ProviderName.OLLAMA)

        elif purpose in STANDARD_TASKS:
            # Cost: Ollama > OpenAI > Claude
            if ProviderName.OLLAMA not in chain:
                chain.append(ProviderName.OLLAMA)
            if sensitivity in (SensitivityLevel.PUBLIC, SensitivityLevel.INTERNAL):
                for p in (ProviderName.OPENAI, ProviderName.CLAUDE):
                    if p not in chain:
                        chain.append(p)

        else:
            # Default: cheapest first
            if ProviderName.OLLAMA not in chain:
                chain.append(ProviderName.OLLAMA)
            if sensitivity in (SensitivityLevel.PUBLIC, SensitivityLevel.INTERNAL):
                for p in (ProviderName.OPENAI, ProviderName.CLAUDE):
                    if p not in chain:
                        chain.append(p)

        # Always add rule-based as final fallback
        if ProviderName.RULE_BASED not in chain:
            chain.append(ProviderName.RULE_BASED)

        # CONFIDENTIAL + non-ONLINE → filter out online providers
        if sensitivity == SensitivityLevel.CONFIDENTIAL and privacy_mode != PrivacyMode.ONLINE_ALLOWED:
            chain = [p for p in chain if not self._providers[p].is_online or p == ProviderName.RULE_BASED]
            if ProviderName.OLLAMA not in chain:
                chain.insert(0, ProviderName.OLLAMA)

        return chain
