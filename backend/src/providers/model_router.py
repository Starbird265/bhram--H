"""
Dynamic Model Router — Zero-Config AI Provider Selection.

Routes AI requests to the best available provider based on:
  1. What's actually running (auto-detected, not hardcoded)
  2. Data sensitivity & privacy mode
  3. Task complexity & content length
  4. User preference (if any)
  5. API key availability (auto-detected from env)

Decision flow:
  1. Discover all providers: Ollama (auto), Claude (if ANTHROPIC_API_KEY), OpenAI (if OPENAI_API_KEY)
  2. Hard rules: RESTRICTED data → local only, embeddings → local only
  3. User override: if CORTEX_PREFERRED_PROVIDER is set, try it first
  4. Task routing: simple → cheapest, complex → best quality
  5. Fallback: always ends at rule_based (never fails)

The router is a pure function — no state, easy to test.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Optional


class TaskType(str, Enum):
    NOISE_FILTER = "noise_filter"
    SUMMARIZE = "summarize"
    DISTILL = "distill"
    CONFLICT = "conflict"
    CLASSIFY = "classify"
    EMBED = "embed"
    PRIVACY_SCAN = "privacy_scan"


class ProviderChoice(str, Enum):
    OLLAMA = "ollama"
    CLAUDE = "claude"
    OPENAI = "openai"
    RULE_BASED = "rule_based"


# Token length thresholds
LONG_CONTENT_THRESHOLD = 1500
SHORT_SUMMARY_THRESHOLD = 800


def _detect_available_providers() -> dict:
    """Auto-detect which providers are actually available right now."""
    available = {"rule_based": True}

    # Ollama: check if the daemon is responding
    try:
        import httpx
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        resp = httpx.get(f"{base_url}/api/tags", timeout=1.0)
        available["ollama"] = resp.status_code == 200
    except Exception:
        available["ollama"] = False

    # Claude: check if API key exists
    available["claude"] = bool(os.getenv("ANTHROPIC_API_KEY", ""))

    # OpenAI: check if API key exists
    available["openai"] = bool(os.getenv("OPENAI_API_KEY", ""))

    return available


def route(
    task: TaskType,
    content_length: int = 0,
    sensitivity: Optional[str] = None,
    ollama_available: Optional[bool] = None,
    claude_available: Optional[bool] = None,
    openai_available: Optional[bool] = None,
    preferred_provider: Optional[str] = None,
) -> ProviderChoice:
    """
    Determine the best provider for a given task.

    If availability params are None, auto-detect from the live system.
    If preferred_provider is set (or env CORTEX_PREFERRED_PROVIDER), try it first.
    """
    # Auto-detect if not explicitly provided
    if ollama_available is None or claude_available is None or openai_available is None:
        detected = _detect_available_providers()
        if ollama_available is None:
            ollama_available = detected.get("ollama", False)
        if claude_available is None:
            claude_available = detected.get("claude", False)
        if openai_available is None:
            openai_available = detected.get("openai", False)

    # User preference from env or argument
    pref = preferred_provider or os.getenv("CORTEX_PREFERRED_PROVIDER", "")
    pref = pref.strip().lower() if pref else ""

    # ── Hard rules ──────────────────────────────────────────────────
    if task == TaskType.EMBED:
        return ProviderChoice.OLLAMA if ollama_available else ProviderChoice.RULE_BASED

    if task == TaskType.PRIVACY_SCAN:
        return ProviderChoice.OLLAMA if ollama_available else ProviderChoice.RULE_BASED

    if sensitivity in ("restricted", "confidential"):
        return ProviderChoice.OLLAMA if ollama_available else ProviderChoice.RULE_BASED

    # ── No AI available at all ──────────────────────────────────────
    if not ollama_available and not claude_available and not openai_available:
        return ProviderChoice.RULE_BASED

    # ── User explicit preference (if available) ─────────────────────
    if pref:
        pref_map = {
            "ollama": (ProviderChoice.OLLAMA, ollama_available),
            "claude": (ProviderChoice.CLAUDE, claude_available),
            "openai": (ProviderChoice.OPENAI, openai_available),
        }
        if pref in pref_map:
            choice, avail = pref_map[pref]
            if avail:
                return choice
            # Preferred not available — fall through to auto-routing

    # ── Task-based routing ───────────────────────────────────────────
    if task == TaskType.NOISE_FILTER:
        return _cheapest(ollama_available, openai_available, claude_available)

    if task == TaskType.CLASSIFY:
        return _cheapest(ollama_available, openai_available, claude_available)

    if task == TaskType.SUMMARIZE:
        if content_length <= SHORT_SUMMARY_THRESHOLD:
            return _cheapest(ollama_available, openai_available, claude_available)
        else:
            return _best_quality(ollama_available, openai_available, claude_available)

    if task == TaskType.DISTILL:
        if content_length <= SHORT_SUMMARY_THRESHOLD:
            return _cheapest(ollama_available, openai_available, claude_available)
        elif content_length <= LONG_CONTENT_THRESHOLD:
            return _cheapest(ollama_available, openai_available, claude_available)
        else:
            return _best_quality(ollama_available, openai_available, claude_available)

    if task == TaskType.CONFLICT:
        return _best_quality(ollama_available, openai_available, claude_available)

    # Default → cheapest
    return _cheapest(ollama_available, openai_available, claude_available)


def _cheapest(ollama: bool, openai: bool, claude: bool) -> ProviderChoice:
    """Cheapest available: Ollama (free) > OpenAI (cheaper) > Claude (expensive)."""
    if ollama:
        return ProviderChoice.OLLAMA
    if openai:
        return ProviderChoice.OPENAI
    if claude:
        return ProviderChoice.CLAUDE
    return ProviderChoice.RULE_BASED


def _best_quality(ollama: bool, openai: bool, claude: bool) -> ProviderChoice:
    """Best quality: Claude > OpenAI > Ollama (local model usually smaller)."""
    if claude:
        return ProviderChoice.CLAUDE
    if openai:
        return ProviderChoice.OPENAI
    if ollama:
        return ProviderChoice.OLLAMA
    return ProviderChoice.RULE_BASED


def describe_routing(
    task: TaskType,
    content_length: int,
    sensitivity: Optional[str],
    ollama_available: Optional[bool] = None,
    claude_available: Optional[bool] = None,
    openai_available: Optional[bool] = None,
    preferred_provider: Optional[str] = None,
) -> dict:
    """Debug helper — returns routing decision with explanation."""
    # Auto-detect for the debug output
    if ollama_available is None or claude_available is None or openai_available is None:
        detected = _detect_available_providers()
        if ollama_available is None:
            ollama_available = detected.get("ollama", False)
        if claude_available is None:
            claude_available = detected.get("claude", False)
        if openai_available is None:
            openai_available = detected.get("openai", False)

    choice = route(
        task, content_length, sensitivity,
        ollama_available, claude_available, openai_available,
        preferred_provider,
    )

    reasons = []
    if sensitivity in ("restricted", "confidential"):
        reasons.append(f"Sensitivity={sensitivity} forces local processing")
    if task in (TaskType.EMBED, TaskType.PRIVACY_SCAN):
        reasons.append(f"Task={task.value} is always local")
    pref = preferred_provider or os.getenv("CORTEX_PREFERRED_PROVIDER", "")
    if pref:
        reasons.append(f"User preference: {pref}")
    if not reasons:
        if choice == ProviderChoice.OLLAMA:
            reasons.append("Cheapest option (free) that meets quality requirements")
        elif choice == ProviderChoice.CLAUDE:
            reasons.append("Complex task requiring high quality (ANTHROPIC_API_KEY detected)")
        elif choice == ProviderChoice.OPENAI:
            reasons.append("Quality task with OpenAI available (OPENAI_API_KEY detected)")
        else:
            reasons.append("No AI provider available — using rule-based fallback")

    return {
        "task": task.value,
        "content_length": content_length,
        "sensitivity": sensitivity,
        "ollama_available": ollama_available,
        "claude_available": claude_available,
        "openai_available": openai_available,
        "preferred_provider": pref or None,
        "chosen_provider": choice.value,
        "reasons": reasons,
        "estimated_cost_usd": _estimate_cost(choice, content_length),
    }


def _estimate_cost(choice: ProviderChoice, content_length: int) -> float:
    """Very rough token cost estimate."""
    if choice in (ProviderChoice.OLLAMA, ProviderChoice.RULE_BASED):
        return 0.0
    estimated_tokens = max(content_length // 4, 100) + 300
    if choice == ProviderChoice.CLAUDE:
        return round(estimated_tokens / 1_000_000 * 3.0, 6)
    if choice == ProviderChoice.OPENAI:
        return round(estimated_tokens / 1_000_000 * 0.15, 6)
    return 0.0
