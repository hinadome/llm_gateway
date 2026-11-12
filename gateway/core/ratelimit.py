"""In-memory sliding-window rate limiter (RPM + TPM).

Keeps per-key deques for the last 60 seconds: one of request timestamps
(for RPM), one of token counts of completed requests (for TPM). The
pre-flight check adds the caller's estimate for the request about to start.

The interface is async so the in-memory and Redis implementations are
interchangeable (``RedisRateLimiter`` in ``redis_backends.py`` shares
windows across workers/replicas; the pipeline only depends on the
``check`` / ``record_request`` / ``record_usage`` methods).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Window:
    requests: deque = field(default_factory=deque)  # deque[float]
    tokens: deque = field(default_factory=deque)  # deque[tuple[float, int]]


class RateLimiter:
    WINDOW_SECONDS = 60.0

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}

    def _window(self, key: str) -> _Window:
        window = self._windows.get(key)
        if window is None:
            window = _Window()
            self._windows[key] = window
        return window

    def _prune(self, window: _Window, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        while window.requests and window.requests[0] <= cutoff:
            window.requests.popleft()
        while window.tokens and window.tokens[0][0] <= cutoff:
            window.tokens.popleft()

    async def check(
        self,
        key: str,
        rpm_limit: int | None,
        tpm_limit: int | None,
        estimated_tokens: int = 0,
    ) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        if rpm_limit is None and tpm_limit is None:
            return True, 0.0
        now = time.monotonic()
        window = self._window(key)
        self._prune(window, now)

        if rpm_limit is not None and len(window.requests) >= rpm_limit:
            oldest = window.requests[0] if window.requests else now
            return False, max(0.0, self.WINDOW_SECONDS - (now - oldest))

        if tpm_limit is not None:
            used = sum(tokens for _, tokens in window.tokens)
            if used + estimated_tokens > tpm_limit:
                oldest = window.tokens[0][0] if window.tokens else now
                return False, max(0.0, self.WINDOW_SECONDS - (now - oldest))
        return True, 0.0

    async def record_request(self, key: str) -> None:
        window = self._window(key)
        now = time.monotonic()
        window.requests.append(now)
        self._prune(window, now)

    async def record_usage(self, key: str, tokens: int) -> None:
        if tokens <= 0:
            return
        window = self._window(key)
        now = time.monotonic()
        window.tokens.append((now, tokens))
        self._prune(window, now)
