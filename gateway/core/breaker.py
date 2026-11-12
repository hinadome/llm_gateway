"""Per-deployment circuit breaker for health-aware routing.

A deployment's circuit moves through three states:

- ``CLOSED``   — normal operation. Each request that exhausts its retry
  budget on this deployment counts as one *consecutive failure*; a success
  resets the count. Reaching ``failure_threshold`` trips the circuit open.
- ``OPEN``     — the router skips the deployment entirely (requests go to
  other members of the tier / lower-priority deployments) until
  ``cooldown_seconds`` elapse. If *every* deployment of a model is open,
  the router ignores the circuits and routes anyway — availability beats
  protection, and an open circuit is evidence, not proof.
- ``HALF_OPEN`` — the cooldown has elapsed; the next request is a probe.
  One success closes the circuit, one failure re-opens it with a fresh
  cooldown. There is no concurrency cap on probes: the cooldown is the
  probe rate limit, and a burst that arrives while the deployment is still
  broken just fails over through the normal retry/fallback path.

Failure accounting is *request-level*: the retries a single request burns
on a deployment count as one failure (retries are the short-term
resilience mechanism; the breaker is the medium-term one). Client-caused
errors (``fallback=False`` — e.g. a 400 for a malformed request) never
reach the breaker: the deployment answered fine, the request was bad.

State is per-process by default: each worker/replica trips and recovers on
its own evidence. The ``RedisCircuitBreakerRegistry`` in ``redis_backends.py``
(``routing.breaker.backend: redis``) is the shared-state counterpart with
the same interface.

The clock is injectable for deterministic tests (monotonic by default);
the ``CircuitBreaker`` state machine itself is synchronous — only the
registry facade is async, so the in-memory and Redis backends are
interchangeable at the router (like the rate limiter and cache backends).
"""

from __future__ import annotations

import time
from enum import IntEnum
from typing import Callable

from ..config import RoutingConfig
from ..observability import metrics


class CircuitState(IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class CircuitBreaker:
    """State machine for one deployment. Owned by CircuitBreakerRegistry."""

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0  # consecutive request-level failures (CLOSED)
        self._opened_at = 0.0
        self.opened_count = 0  # lifetime trips (survives resets; ops signal)
        # Wired by the registry: mirrors transitions into Prometheus.
        self._on_change: Callable[[CircuitState], None] = lambda new: None

    # -- state ----------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Effective state — OPEN past its cooldown reads as HALF_OPEN.

        Pure (no transition side effects), so gauges and admin snapshots
        can report the state the *next* request would see.
        """
        if (
            self._state is CircuitState.OPEN
            and self._clock() - self._opened_at >= self._cooldown
        ):
            return CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """May the router send a request to this deployment now?"""
        if self._state is CircuitState.OPEN:
            if self._clock() - self._opened_at >= self._cooldown:
                self._transition(CircuitState.HALF_OPEN)
                return True
            return False
        return True

    # -- accounting -------------------------------------------------------

    def record_success(self) -> None:
        """A request served by this deployment completed (first chunk, for streams).

        Any success — probe or all-open bypass — is evidence of health:
        close the circuit.
        """
        if self._state is not CircuitState.CLOSED:
            self._transition(CircuitState.CLOSED)
        self._failures = 0

    def record_failure(self) -> None:
        """A request exhausted its retry budget on this deployment.

        Assumes the request went through ``allow()`` (the router only
        records for deployments it picked), so OPEN here means the
        all-open bypass — already open, nothing new to learn, and the
        cooldown should not be extended.
        """
        if self._state is CircuitState.OPEN:
            return
        if self._state is CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip()

    def reset(self) -> None:
        """Force-close (admin action); lifetime trip count is kept."""
        self._transition(CircuitState.CLOSED)
        self._failures = 0

    def snapshot(self) -> dict:
        state = self.state
        cooldown_remaining = 0.0
        if state is CircuitState.OPEN:
            cooldown_remaining = max(
                0.0, self._cooldown - (self._clock() - self._opened_at)
            )
        return {
            "state": state.name.lower(),
            "consecutive_failures": self._failures,
            "opened_count": self.opened_count,
            "cooldown_remaining_seconds": round(cooldown_remaining, 3),
        }

    # -- internals ----------------------------------------------------------

    def _trip(self) -> None:
        self._opened_at = self._clock()
        self.opened_count += 1
        self._failures = 0
        self._transition(CircuitState.OPEN)

    def _transition(self, new: CircuitState) -> None:
        if new is self._state:
            return
        self._state = new
        self._on_change(new)


class CircuitBreakerRegistry:
    """One breaker per configured deployment + their Prometheus series.

    The in-process backend. ``RedisCircuitBreakerRegistry``
    (redis_backends.py) implements the same async interface over shared
    state; the router, pipeline and admin API talk to either
    interchangeably (selected by ``routing.breaker.backend``).
    """

    def __init__(self, routing: RoutingConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.enabled = routing.breaker.enabled
        self._threshold = routing.breaker.failure_threshold
        self._cooldown = routing.breaker.cooldown_seconds
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._providers: dict[str, str] = {}

    def register(self, name: str, provider: str) -> CircuitBreaker:
        if name in self._breakers:
            # Hot-reload: a deployment that already has a breaker keeps
            # its state — reloading config is not a health event (an
            # open circuit must not reset just because config changed).
            self._providers[name] = provider
            return self._breakers[name]
        breaker = CircuitBreaker(
            name, self._threshold, self._cooldown, self._clock
        )

        def on_change(new: CircuitState) -> None:
            metrics.CIRCUIT_STATE.labels(deployment=name, provider=provider).set(
                int(new)
            )
            metrics.CIRCUIT_TRANSITIONS.labels(
                deployment=name, provider=provider, state=new.name.lower()
            ).inc()

        breaker._on_change = on_change
        self._breakers[name] = breaker
        self._providers[name] = provider
        # Create the gauge series from boot (visible on dashboards even
        # before the first transition).
        metrics.CIRCUIT_STATE.labels(deployment=name, provider=provider).set(
            CircuitState.CLOSED
        )
        return breaker

    # -- router-facing API (no-ops when disabled) -------------------------

    async def allow(self, name: str) -> bool:
        if not self.enabled:
            return True
        breaker = self._breakers.get(name)
        return breaker.allow() if breaker is not None else True

    async def record_success(self, name: str) -> None:
        if not self.enabled:
            return
        if breaker := self._breakers.get(name):
            breaker.record_success()

    async def record_failure(self, name: str) -> None:
        if not self.enabled:
            return
        if breaker := self._breakers.get(name):
            breaker.record_failure()

    # -- admin ------------------------------------------------------------

    async def reset(self, name: str) -> None:
        breaker = self._breakers.get(name)
        if breaker is None:
            raise KeyError(name)
        breaker.reset()

    async def snapshots(self) -> dict[str, dict]:
        return {name: breaker.snapshot() for name, breaker in self._breakers.items()}

    # -- observability ------------------------------------------------------

    async def refresh_gauges(self) -> None:
        """Re-sync gauges with effective state.

        OPEN → HALF_OPEN happens on the clock, without a transition event
        to observe; scrapes and admin reads call this first so the gauge
        matches what the next request would actually see.
        """
        for name, breaker in self._breakers.items():
            metrics.CIRCUIT_STATE.labels(
                deployment=name, provider=self._providers[name]
            ).set(int(breaker.state))
