"""
Phase 4 — Rate Limiting Middleware

Per-IP and per-connector rate limiting using a sliding window algorithm.
All state stored in-process (no Redis required) — uses a thread-safe
token bucket / sliding window counter backed by a dict.

Limits:
  Webhooks:         100 req/min per IP  (DDoS protection)
  OAuth endpoints:  20 req/min per IP   (prevents token spray)
  Sync endpoints:   5 req/min per app   (prevents runaway sync loops)
  General API:      300 req/min per IP  (normal usage cap)

Algorithm: Sliding window counter
  - Track (ip, window_start) → request count
  - Window = 60s rolling window
  - Thread-safe via defaultdict + Lock

Usage (in api.py):
  from middleware.rate_limiter import RateLimiter
  app.add_middleware(RateLimiter)
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Dict, Tuple, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


# ── Rate limit rules ────────────────────────────────────────────────────────────
# (path_prefix, limit_per_minute, window_seconds)
RULES: list[Tuple[str, int, int]] = [
    ("/webhooks/",        100, 60),   # Webhooks — generous for event bursts
    ("/api/oauth/",       20,  60),   # OAuth — tight to prevent spray attacks
    ("/api/sync/run/",    5,   60),   # Sync — prevent runaway loops
    ("/api/providers/",   60,  60),   # Provider calls
    ("/",                 300, 60),   # Global fallback
]


def _get_rule(path: str) -> Tuple[int, int]:
    """Return (limit, window_seconds) for the most specific matching rule."""
    for prefix, limit, window in RULES:
        if path.startswith(prefix):
            return limit, window
    return 300, 60  # Default


# ── Sliding window store ────────────────────────────────────────────────────────

class _SlidingWindow:
    """
    Thread-safe sliding window rate limiter.
    Uses a 2-bucket approach (current + previous window) for accuracy
    without the memory overhead of per-request timestamps.
    """
    def __init__(self):
        # {key: {window_ts: count}}
        self._buckets: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self._lock = Lock()

    def check_and_increment(self, key: str, limit: int, window: int) -> Tuple[bool, int]:
        """
        Returns (allowed, remaining_after_this_request).
        If allowed=False, the request should be rejected.
        """
        now = time.time()
        current_window = int(now // window) * window
        prev_window = current_window - window
        # Weight from previous window (sliding fraction)
        elapsed = now - current_window
        prev_weight = max(0.0, 1.0 - elapsed / window)

        with self._lock:
            buckets = self._buckets[key]
            current_count = buckets.get(current_window, 0)
            prev_count = buckets.get(prev_window, 0)

            # Weighted count = previous window count * remaining weight + current
            weighted = int(prev_count * prev_weight) + current_count

            if weighted >= limit:
                return False, 0

            # Increment current window
            buckets[current_window] = current_count + 1

            # Cleanup old windows (older than 2 windows)
            cutoff = prev_window
            old_keys = [k for k in buckets if k < cutoff]
            for k in old_keys:
                del buckets[k]

            remaining = max(0, limit - weighted - 1)
            return True, remaining


_window = _SlidingWindow()


# ── Middleware ──────────────────────────────────────────────────────────────────

class RateLimiter(BaseHTTPMiddleware):
    """
    FastAPI middleware that enforces sliding-window rate limits.

    On limit exceeded → 429 JSON with Retry-After header.
    On all other requests → adds X-RateLimit-* headers.
    """

    # Paths that are always exempt (health checks, static files)
    EXEMPT_PREFIXES = ("/health", "/static", "/favicon", "/_")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Exempt health checks and static files
        for exempt in self.EXEMPT_PREFIXES:
            if path.startswith(exempt):
                return await call_next(request)

        # Get client IP (respects X-Forwarded-For behind proxies)
        client_ip = self._get_client_ip(request)
        limit, window = _get_rule(path)
        key = f"{client_ip}:{path.split('/')[1] if '/' in path else 'root'}"

        allowed, remaining = _window.check_and_increment(key, limit, window)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "message": f"Too many requests. Limit: {limit} per {window}s.",
                    "retry_after": window,
                },
                headers={
                    "Retry-After": str(window),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Window": str(window),
                },
            )

        response = await call_next(request)

        # Add rate limit headers to all responses
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"] = str(window)
        return response

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """Extract real client IP, respecting X-Forwarded-For."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"


# ── Per-connector sync limiter (standalone, not middleware) ─────────────────────

class ConnectorRateLimiter:
    """
    Simple per-connector rate limiter for the SyncManager.
    Prevents a single connector from being synced more than once per window.

    Usage:
        limiter = ConnectorRateLimiter()
        if not limiter.allow("slack"):
            raise HTTPException(429, "Slack sync already running")
        # ... run sync ...
    """

    def __init__(self, min_gap_seconds: int = 60):
        self._last_run: Dict[str, float] = {}
        self._lock = Lock()
        self._min_gap = min_gap_seconds

    def allow(self, app_id: str) -> bool:
        with self._lock:
            last = self._last_run.get(app_id, 0.0)
            if time.time() - last < self._min_gap:
                return False
            self._last_run[app_id] = time.time()
            return True

    def status(self) -> Dict[str, float]:
        """Return seconds since last sync per connector."""
        now = time.time()
        with self._lock:
            return {k: round(now - v, 1) for k, v in self._last_run.items()}


# Singleton for use across api.py
connector_limiter = ConnectorRateLimiter(min_gap_seconds=60)
