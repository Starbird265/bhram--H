"""
Ollama Provider Adapter — Fully Dynamic, Zero Config.

Auto-discovers all available models from the running Ollama instance.
Picks the best chat and embedding model automatically based on:
  - Parameter count (largest = most capable for chat)
  - Model family tags (embedding models detected by name/family)
  - Re-discovers every 60s so new models are picked up live

No hardcoded model names. No manual configuration. Just works.
"""

import os
import time
import json
import httpx
from typing import List, Optional, Any, Dict

from providers import (
    AIProvider, AIRequest, AIResponse, EmbedResponse,
    ProviderName,
)


# Keywords that indicate an embedding-capable model
_EMBED_KEYWORDS = {"embed", "nomic", "bge", "e5", "gte", "minilm", "all-minilm"}


class OllamaAdapter:
    """Ollama local LLM adapter — fully dynamic model discovery."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        chat_model: Optional[str] = None,
        embed_model: Optional[str] = None,
    ):
        self._base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

        # If user explicitly passes models, use them (escape hatch).
        # Otherwise, they will be auto-discovered on first use.
        self._explicit_chat_model = chat_model
        self._explicit_embed_model = embed_model

        # Discovery cache
        self._discovered_models: List[Dict[str, Any]] = []
        self._best_chat_model: Optional[str] = None
        self._best_embed_model: Optional[str] = None
        self._discovery_ts: float = 0.0
        self._DISCOVERY_TTL: float = 60.0  # re-discover every 60s

        # Availability cache
        self._available: Optional[bool] = None
        self._available_checked_at: float = 0.0
        self._AVAILABILITY_TTL: float = 60.0

    # ─── Dynamic Discovery ──────────────────────────────────────

    def _discover_models(self) -> List[Dict[str, Any]]:
        """Hit Ollama /api/tags and cache the result."""
        now = time.time()
        if self._discovery_ts > 0 and (now - self._discovery_ts) < self._DISCOVERY_TTL:
            return self._discovered_models

        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=3.0)
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models", [])
            self._discovered_models = models
            self._discovery_ts = now

            # Auto-select best models
            self._best_chat_model = self._pick_best_chat(models)
            self._best_embed_model = self._pick_best_embed(models)

            if self._best_chat_model:
                print(f"  [Ollama] Auto-discovered chat model: {self._best_chat_model}")
            if self._best_embed_model:
                print(f"  [Ollama] Auto-discovered embed model: {self._best_embed_model}")

            return models
        except Exception:
            self._discovery_ts = now  # Prevent hammering when Ollama is offline or returns error


            return self._discovered_models  # Return stale cache if any

    @staticmethod
    def _pick_best_chat(models: List[Dict[str, Any]]) -> Optional[str]:
        """Pick the best chat model using a two-tier strategy:

        Tier 1 — Local models within the RAM safety cap (OLLAMA_MAX_MODEL_SIZE_GB, default 8 GB).
                   Largest safe local model wins.
        Tier 2 — Cloud/remote models (remote_host field present, size ≈ 0).
                   Used when no safe local model exists. Zero local RAM consumed.

        Filters out embedding-only models in both tiers.
        """
        max_size_gb = float(os.getenv("OLLAMA_MAX_MODEL_SIZE_GB", "8"))
        max_size_bytes = max_size_gb * 1_000_000_000

        local_candidates = []
        cloud_candidates = []

        for m in models:
            name = m.get("name", "").lower()
            # Skip embedding-specific models
            if any(kw in name for kw in _EMBED_KEYWORDS):
                continue

            is_cloud = bool(m.get("remote_host"))
            size_bytes = m.get("size", 0)

            if is_cloud:
                # Cloud models: no local RAM cost
                param_str = m.get("details", {}).get("parameter_size", "0B")
                try:
                    param_num = float(param_str.replace("B", "").replace("M", "").strip())
                    if "M" in param_str:
                        param_num /= 1000
                except (ValueError, TypeError):
                    param_num = 0.0
                cloud_candidates.append((m.get("name"), param_num))
            else:
                # Local models: skip if too small (no weights) or too large (OOM)
                if size_bytes < 1_000_000:
                    continue
                if size_bytes > max_size_bytes:
                    gb = size_bytes / 1_000_000_000
                    print(f"  [Ollama] Local model {m.get('name')} ({gb:.1f} GB) exceeds {max_size_gb} GB cap — skipping local, will try cloud")
                    continue

                param_str = m.get("details", {}).get("parameter_size", "0B")
                try:
                    param_num = float(param_str.replace("B", "").replace("M", "").strip())
                    if "M" in param_str:
                        param_num /= 1000
                except (ValueError, TypeError):
                    param_num = 0.0
                local_candidates.append((m.get("name"), param_num, size_bytes))

        # Tier 1: prefer largest safe local model
        if local_candidates:
            local_candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
            return local_candidates[0][0]

        # Tier 2: fall back to cloud model (largest by param count)
        if cloud_candidates:
            cloud_candidates.sort(key=lambda x: x[1], reverse=True)
            chosen = cloud_candidates[0][0]
            print(f"  [Ollama] No safe local model — using cloud model: {chosen} (zero local RAM)")
            return chosen

        return None

    @staticmethod
    def _pick_best_embed(models: List[Dict[str, Any]]) -> Optional[str]:
        """Pick the best embedding model by name pattern."""
        for m in models:
            name = m.get("name", "").lower()
            if any(kw in name for kw in _EMBED_KEYWORDS):
                return m.get("name")
        return None

    @property
    def _chat_model(self) -> str:
        """The active chat model — auto-discovered or explicit."""
        if self._explicit_chat_model:
            return self._explicit_chat_model
        if not self._best_chat_model:
            self._discover_models()
        return self._best_chat_model or "llama3.2"  # ultimate fallback name

    @property
    def _embed_model(self) -> str:
        """The active embed model — auto-discovered or explicit."""
        if self._explicit_embed_model:
            return self._explicit_embed_model
        if not self._best_embed_model:
            self._discover_models()
        return self._best_embed_model or "nomic-embed-text"

    # ─── AIProvider Protocol ────────────────────────────────────

    @property
    def name(self) -> ProviderName:
        return ProviderName.OLLAMA

    @property
    def is_online(self) -> bool:
        return False  # Local provider

    @property
    def is_available(self) -> bool:
        """Check if Ollama daemon is reachable. Result cached for 60 seconds."""
        now = time.time()
        if self._available is not None and (now - self._available_checked_at) < self._AVAILABILITY_TTL:
            return self._available
        try:
            resp = httpx.get(f"{self._base_url}/api/tags", timeout=2.0)
            self._available = resp.status_code == 200
            if self._available:
                # Piggyback discovery on availability check
                data = resp.json()
                models = data.get("models", [])
                self._discovered_models = models
                self._discovery_ts = now
                self._best_chat_model = self._pick_best_chat(models)
                self._best_embed_model = self._pick_best_embed(models)
        except Exception:
            self._available = False
        self._available_checked_at = now
        return self._available

    @property
    def supports_structured_output(self) -> bool:
        return True

    @property
    def max_context_tokens(self) -> int:
        return 8192  # Conservative default; varies by model

    def get_discovered_models(self) -> List[Dict[str, Any]]:
        """Return all discovered models — used by API endpoints for transparency."""
        self._discover_models()
        return [
            {
                "name": m.get("name"),
                "parameter_size": m.get("details", {}).get("parameter_size", "unknown"),
                "family": m.get("details", {}).get("family", "unknown"),
                "quantization": m.get("details", {}).get("quantization_level", "unknown"),
                "size_bytes": m.get("size", 0),
                "is_chat_model": m.get("name") == self._best_chat_model,
                "is_embed_model": m.get("name") == self._best_embed_model,
                "is_local": not bool(m.get("remote_host")),
                "is_cloud": bool(m.get("remote_host")),
                "remote_host": m.get("remote_host"),
            }
            for m in self._discovered_models
        ]

    async def complete(self, request: AIRequest) -> AIResponse:
        """Send a chat completion to local Ollama."""
        if not self.is_available:
            return AIResponse(
                provider=self.name, model=self._chat_model,
                error="Ollama not available at " + self._base_url,
            )

        start = time.time()
        try:
            payload = {
                "model": self._chat_model,
                "messages": request.messages,
                "stream": False,
                "options": {"temperature": request.temperature},
            }

            # Add structured output format if schema provided
            if request.response_schema:
                try:
                    schema = request.response_schema.model_json_schema()
                    payload["format"] = schema
                except Exception:
                    payload["format"] = "json"

            # Use OLLAMA_TIMEOUT_SEC env override (default 30s — prevents device freeze on large models)
            _timeout_sec = float(os.getenv("OLLAMA_TIMEOUT_SEC", "30"))
            async with httpx.AsyncClient(timeout=_timeout_sec) as client:
                resp = await client.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            content = data.get("message", {}).get("content", "")
            parsed = None

            # Try to parse structured output
            if request.response_schema and content:
                try:
                    raw = json.loads(content)
                    parsed = request.response_schema(**raw)
                except Exception:
                    pass

            # Token counts from Ollama response
            input_tok = data.get("prompt_eval_count", 0)
            output_tok = data.get("eval_count", 0)

            return AIResponse(
                provider=self.name, model=self._chat_model,
                content=content, parsed=parsed,
                input_tokens=input_tok, output_tokens=output_tok,
                cost_usd=0.0,  # Local = free
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return AIResponse(
                provider=self.name, model=self._chat_model,
                error=str(e),
                latency_ms=(time.time() - start) * 1000,
            )

    async def embed(self, texts: List[str]) -> EmbedResponse:
        """Generate embeddings via Ollama."""
        if not self.is_available:
            return EmbedResponse(
                provider=self.name, model=self._embed_model,
                embeddings=[], dimensions=0,
            )

        # If no embedding model discovered, try using the chat model
        # (some chat models support embeddings too)
        embed_model = self._embed_model

        try:
            embeddings = []
            for text in texts:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/embed",
                        json={"model": embed_model, "input": text},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    # Ollama returns {"embeddings": [[...], ...]}
                    emb = data.get("embeddings", [[]])[0]
                    embeddings.append(emb)

            dims = len(embeddings[0]) if embeddings and embeddings[0] else 0
            return EmbedResponse(
                provider=self.name, model=embed_model,
                embeddings=embeddings, dimensions=dims,
                cost_usd=0.0,
            )
        except Exception:
            return EmbedResponse(
                provider=self.name, model=embed_model,
                embeddings=[], dimensions=0,
            )

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Local models are free."""
        return 0.0
