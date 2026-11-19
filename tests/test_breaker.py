"""Circuit breakers: state machine units + router/pipeline/admin integration.

App-level tests use ``retry_backoff_base=0`` so the retry sleeps vanish;
cooldowns are either the default 30s (long enough never to elapse during a
test) or 0s (half-open immediately) — no real waiting anywhere.
"""

from __future__ import annotations

import pytest

from gateway.config import BreakerConfig, RoutingConfig
from gateway.core.breaker import CircuitBreaker, CircuitBreakerRegistry, CircuitState
from gateway.observability import metrics

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


class Clock:
    """Controllable clock: cooldowns become deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ----------------------------------------------------------------------
# State machine (unit)
# ----------------------------------------------------------------------


def test_opens_after_consecutive_failures():
    clock = Clock()
    breaker = CircuitBreaker("d", failure_threshold=3, cooldown_seconds=30, clock=clock)
    assert breaker.allow() is True
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()  # threshold reached
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False


def test_success_resets_failure_count():
    breaker = CircuitBreaker("d", failure_threshold=3, cooldown_seconds=30, clock=Clock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED  # 2 < 3: count restarted


def test_open_blocks_until_cooldown_then_half_open():
    clock = Clock()
    breaker = CircuitBreaker("d", failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure()
    clock.advance(29)
    assert breaker.allow() is False
    # the pure `state` view reports the half-open the *next* request sees,
    # without mutating anything
    clock.advance(1)
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow() is True  # probe allowed


def test_half_open_probe_success_closes():
    clock = Clock()
    breaker = CircuitBreaker("d", failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure()
    clock.advance(30)
    assert breaker.allow() is True  # probe
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True


def test_half_open_probe_failure_reopens_with_fresh_cooldown():
    clock = Clock()
    breaker = CircuitBreaker("d", failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure()
    clock.advance(30)
    assert breaker.allow() is True  # probe
    breaker.record_failure()  # probe failed -> open again, cooldown restarted
    clock.advance(29)
    assert breaker.allow() is False
    clock.advance(1)
    assert breaker.allow() is True


def test_failure_while_open_does_not_extend_cooldown():
    """All-open bypass: failures on an already-open circuit add nothing."""
    clock = Clock()
    breaker = CircuitBreaker("d", failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure()
    clock.advance(1)
    breaker.record_failure()  # request routed through the bypass and failed
    clock.advance(29)  # only the *original* cooldown elapsed
    assert breaker.allow() is True


def test_opened_count_survives_recovery_and_reset():
    breaker = CircuitBreaker("d", failure_threshold=1, cooldown_seconds=30, clock=Clock())
    breaker.record_failure()
    breaker.reset()
    breaker.record_failure()
    assert breaker.opened_count == 2
    assert breaker.snapshot()["opened_count"] == 2


def test_snapshot_reports_cooldown_remaining():
    clock = Clock()
    breaker = CircuitBreaker("d", failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure()
    clock.advance(10)
    snap = breaker.snapshot()
    assert snap["state"] == "open"
    assert snap["consecutive_failures"] == 0
    assert 19 < snap["cooldown_remaining_seconds"] <= 20


async def test_registry_disabled_never_blocks():
    registry = CircuitBreakerRegistry(BreakerConfig(enabled=False), clock=Clock())
    registry.register("d", "openai")
    for _ in range(10):
        await registry.record_failure("d")
    assert await registry.allow("d") is True
    assert (await registry.snapshots())["d"]["state"] == "closed"


# ----------------------------------------------------------------------
# App-level env: fast retries, deterministic breaker settings
# ----------------------------------------------------------------------


async def _make_breaker_env(tmp_path, **breaker_kwargs):
    config = make_config(
        routing=RoutingConfig(
            retry_backoff_base=0.0,
            breaker=BreakerConfig(**breaker_kwargs),
        )
    )
    async for ns in _make_env(tmp_path, config=config):
        yield ns


@pytest.fixture
async def breaker_env(tmp_path):
    """Default breaker: threshold 3, cooldown 30s (never elapses in a test)."""
    async for ns in _make_breaker_env(tmp_path, failure_threshold=3, cooldown_seconds=30.0):
        yield ns


@pytest.fixture
async def fast_cooldown_env(tmp_path):
    """Cooldown 0: an open circuit half-opens on the very next request."""
    async for ns in _make_breaker_env(tmp_path, failure_threshold=3, cooldown_seconds=0.0):
        yield ns


async def _chat(env, key: str, content: str):
    """One non-streaming request (unique content: never a cache hit)."""
    return await env.client.post(
        "/v1/chat/completions", json=chat_body(content=content), headers=auth(key)
    )


async def _breaker_state(env, deployment: str) -> dict:
    routing = (
        await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))
    ).json()
    return routing["circuit_breaker"]["deployments"][deployment]


async def _trip_primary(env, key: str) -> None:
    env.mock.fail_counts["model-a"] = 100
    for i in range(3):  # threshold 3: three request-level failures on primary
        resp = await _chat(env, key, f"trip probe {i}")
        assert resp.status_code == 200
        assert resp.headers["X-Gateway-Deployment"] == "openai/fallback"


# ----------------------------------------------------------------------
# Routing behavior (non-streaming + streaming)
# ----------------------------------------------------------------------


async def test_open_circuit_skips_deployment(breaker_env):
    key = (await create_key(breaker_env.client, "cb-skip"))["key"]
    env = breaker_env
    await _trip_primary(env, key)

    # the circuit is open: primary receives no traffic at all, and the
    # fallback answers without burning a single retry
    before = len(env.mock.requests)
    resp = await _chat(env, key, "after trip")
    assert resp.status_code == 200
    assert resp.headers["X-Gateway-Deployment"] == "openai/fallback"
    assert [b["model"] for b in env.mock.bodies()[before:]] == ["model-b"]

    # visible to operators and on the gauge
    state = await _breaker_state(env, "openai/primary")
    assert state["state"] == "open"
    assert state["opened_count"] == 1
    assert (
        metrics.CIRCUIT_STATE.labels(
            deployment="openai/primary", provider="openai"
        )._value.get()
        == int(CircuitState.OPEN)
    )


async def test_circuit_recovers_after_cooldown(fast_cooldown_env):
    env = fast_cooldown_env
    key = (await create_key(env.client, "cb-recover"))["key"]
    await _trip_primary(env, key)
    env.mock.fail_counts["model-a"] = 0  # upstream healthy again

    # cooldown 0 -> next request is a probe; it succeeds -> circuit closes
    resp = await _chat(env, key, "recovered")
    assert resp.status_code == 200
    assert resp.headers["X-Gateway-Deployment"] == "openai/primary"
    state = await _breaker_state(env, "openai/primary")
    assert state["state"] == "closed"
    assert state["consecutive_failures"] == 0

    # and stays closed: the next request also goes to the primary
    resp = await _chat(env, key, "still recovered")
    assert resp.headers["X-Gateway-Deployment"] == "openai/primary"


async def test_half_open_probe_failure_reopens(fast_cooldown_env):
    """A failed probe re-trips the circuit (fresh-cooldown semantics are
    unit-tested above; with cooldown 0 every request is a probe)."""
    env = fast_cooldown_env
    key = (await create_key(env.client, "cb-reopen"))["key"]
    await _trip_primary(env, key)  # circuit open, fail_counts still 100

    # the next request probes primary, which is still broken: reopens
    resp = await _chat(env, key, "probe fails")
    assert resp.status_code == 200  # served by the fallback
    assert (await _breaker_state(env, "openai/primary"))["opened_count"] == 2


async def test_all_circuits_open_still_routes(breaker_env):
    """When every deployment is open, the router tries anyway."""
    env = breaker_env
    key = (await create_key(env.client, "cb-all-open"))["key"]
    env.mock.fail_counts["model-a"] = 100
    env.mock.fail_counts["model-b"] = 100

    for i in range(3):  # both circuits trip (3 failures each)
        resp = await _chat(env, key, f"down {i}")
        assert resp.status_code in (500, 502, 503)

    before = len(env.mock.requests)
    resp = await _chat(env, key, "all open, still trying")
    assert resp.status_code in (500, 502, 503)
    # it genuinely attempted upstream: full retry budget on both deployments
    models = [b["model"] for b in env.mock.bodies()[before:]]
    assert models.count("model-a") == 3
    assert models.count("model-b") == 3


async def test_all_open_bypass_success_closes(breaker_env):
    env = breaker_env
    key = (await create_key(env.client, "cb-bypass"))["key"]
    env.mock.fail_counts["model-a"] = 100
    env.mock.fail_counts["model-b"] = 100
    for i in range(3):
        await _chat(env, key, f"down {i}")

    env.mock.fail_counts["model-a"] = 0
    env.mock.fail_counts["model-b"] = 0
    resp = await _chat(env, key, "bypass recovery")
    assert resp.status_code == 200
    # routed through the all-open bypass to the primary, which succeeded
    assert resp.headers["X-Gateway-Deployment"] == "openai/primary"
    assert (await _breaker_state(env, "openai/primary"))["state"] == "closed"


async def test_streaming_trips_circuit_and_fails_over(breaker_env):
    env = breaker_env
    key = (await create_key(env.client, "cb-stream"))["key"]
    env.mock.fail_counts["model-a"] = 100

    for i in range(3):  # three streamed request-level failures on primary
        async with env.client.stream(
            "POST",
            "/v1/chat/completions",
            json=chat_body(content=f"stream trip {i}", stream=True),
            headers=auth(key),
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["X-Gateway-Deployment"] == "openai/fallback"

    before = len(env.mock.requests)
    async with env.client.stream(
        "POST",
        "/v1/chat/completions",
        json=chat_body(content="stream after trip", stream=True),
        headers=auth(key),
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["X-Gateway-Deployment"] == "openai/fallback"
        text = await resp.aread()
    assert b"[DONE]" in text
    assert [b["model"] for b in env.mock.bodies()[before:]] == ["model-b"]


# ----------------------------------------------------------------------
# Admin + metrics surface
# ----------------------------------------------------------------------


async def test_admin_routing_shows_breaker_state(breaker_env):
    env = breaker_env
    key = (await create_key(env.client, "cb-admin"))["key"]
    routing = (await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))).json()

    assert routing["circuit_breaker"]["enabled"] is True
    test_model = next(m for m in routing["models"] if m["name"] == "test-model")
    primary = next(d for d in test_model["deployments"] if d["name"] == "openai/primary")
    assert primary["breaker"]["state"] == "closed"
    assert primary["breaker"]["opened_count"] == 0

    await _trip_primary(env, key)
    routing = (await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))).json()
    assert (
        routing["circuit_breaker"]["deployments"]["openai/primary"]["state"] == "open"
    )


async def test_admin_reset_reopens_traffic_before_cooldown(breaker_env):
    env = breaker_env
    key = (await create_key(env.client, "cb-reset"))["key"]
    await _trip_primary(env, key)  # cooldown 30s has NOT elapsed

    resp = await env.client.post(
        "/admin/routing/breakers/openai/primary/reset", headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200
    assert resp.json()["breaker"]["state"] == "closed"

    # primary receives traffic again immediately (fail_counts still high)
    before = len(env.mock.requests)
    resp = await _chat(env, key, "after reset")
    assert resp.status_code == 200
    models = [b["model"] for b in env.mock.bodies()[before:]]
    assert models[:3] == ["model-a", "model-a", "model-a"]  # retried, then fell back

    # unknown deployment -> 404
    resp = await env.client.post(
        "/admin/routing/breakers/nope/reset", headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 404


async def test_metrics_expose_circuit_state(fast_cooldown_env):
    env = fast_cooldown_env
    key = (await create_key(env.client, "cb-metrics"))["key"]
    await _trip_primary(env, key)

    # the trip set the gauge to open...
    gauge = metrics.CIRCUIT_STATE.labels(deployment="openai/primary", provider="openai")
    assert gauge._value.get() == int(CircuitState.OPEN)

    # ...but with cooldown 0 the effective state is half-open, and the
    # scrape endpoint re-syncs the gauge before exposing it
    text = (await env.client.get("/metrics")).text
    assert (
        'gateway_circuit_state{deployment="openai/primary",provider="openai"} 1.0' in text
    )
    assert "gateway_circuit_transitions_total" in text


async def test_circuit_open_is_a_trace_event(tmp_path, span_exporter, tracer_provider):
    config = make_config(
        routing=RoutingConfig(
            retry_backoff_base=0.0,
            breaker=BreakerConfig(failure_threshold=3, cooldown_seconds=30.0),
        )
    )
    async for ns in _make_env(tmp_path, tracer_provider=tracer_provider, config=config):
        key = (await create_key(ns.client, "cb-trace"))["key"]
        ns.mock.fail_counts["model-a"] = 100
        for i in range(3):
            resp = await _chat(ns, key, f"trace trip {i}")
            assert resp.status_code == 200

        resp = await _chat(ns, key, "traced skip")
        assert resp.status_code == 200

        chats = [s for s in span_exporter.get_finished_spans() if s.name == "gateway.chat"]
        skipped = [
            e for e in chats[-1].events if e.name == "gateway.circuit_open"
        ]
        assert skipped
        assert skipped[0].attributes["gateway.deployment"] == "openai/primary"
