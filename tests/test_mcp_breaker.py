"""Per-server MCP circuit breakers — tier 66.

The MCP proxy shares its policy spine with chat but, until this tier,
had no answer to a dead tool server: every message burned the full
upstream timeout HOLDING a concurrency slot, and a fleet of agents
hammering one dead upstream degraded the whole surface. The tier adds
the deployment breaker's state machine (``core/breaker.py`` — one
registry per surface, keyed by SERVER name, gauges shared) with
MCP-specific accounting, pinned here:

* WHAT TRIPS: an upstream connect error (``httpx.ConnectError`` /
  ``ConnectTimeout`` — unreachable) or an HTTP 5xx answer (up and
  broken), ``failure_threshold`` consecutive, a success in between
  resets the count. 4xx answers and JSON-RPC error frames in 200
  bodies are the server answering fine — evidence of health.
  Latency NEVER trips (read timeouts are not recorded: tools
  legitimately run long).
* WHAT AN OPEN CIRCUIT DOES: refuses the message in-band — HTTP 200,
  JSON-RPC ``-32004``, the client's id echoed — BEFORE the rate limit
  and the concurrency slot (fail fast, hold nothing). No all-open
  bypass: this server is the only upstream for its name. initialize
  is gated too — after the cooldown it is the natural probe.
* RECOVERY: cooldown elapses → half-open → the next message probes;
  one success closes, one failure re-opens (the shared state
  machine). ``POST /admin/mcp/breakers/{server}/reset`` force-closes
  (operator fixed the server); ``GET /admin/mcp`` reports the
  snapshot; ``/readyz`` counts open MCP circuits informationally.
* OBSERVABILITY: the row is ``status=error`` (the client experienced
  a failure — the SLO error-rate must count it) while the request
  metric's outcome is the honest ``circuit_open``; the gauges are the
  same ``gateway_circuit_state`` family the deployment breakers use,
  provider="mcp".
* SHARED STATE: ``mcp.breaker.backend: redis`` swaps in the Redis
  registry under its OWN keyspace (namespace mcp-cb), so a fleet
  trips together and a deployment sharing a server's name never
  collides with it.
"""

from __future__ import annotations

import httpx

from gateway.config import (
    BreakerConfig,
    MCPConfig,
    MCPServerConfig,
)
from gateway.core.mcp import CIRCUIT_OPEN
from gateway.observability import metrics

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_mcp import MockMCP, _init_body
from .test_mcp import _post as _post_to

# The breaker tests' own upstream name: distinct from test_mcp's
# "test-server" so the shared Prometheus counters read cleanly.
SERVER = MCPServerConfig(name="brk-server", url="http://mcp.test/mcp")


async def _post(client, body, key, **kw):
    """_post pointed at THIS file's server (test_mcp's helper defaults
    to its own 'test-server')."""
    return await _post_to(client, body, key, server=SERVER.name, **kw)


def _mcp_config(**breaker_kwargs) -> MCPConfig:
    knobs: dict = {
        "failure_threshold": 1,
        "cooldown_seconds": 30.0,
    }
    knobs.update(breaker_kwargs)
    return MCPConfig(
        enabled=True,
        servers=[SERVER],
        breaker=BreakerConfig(**knobs),
    )


class FailingMCP:
    """A scripted MCP upstream that can fail its NEXT call a chosen
    way before ever answering, then falls back to the healthy MockMCP
    behavior. ``requests`` counts every call that REACHED the upstream
    (the failing ones included); the breaker's fast refusals never
    land here, so a frozen count is the fail-fast proof."""

    def __init__(self) -> None:
        self.healthy = MockMCP()
        self.requests: list[httpx.Request] = []
        # how the NEXT call fails: "connect" | "connect_timeout" |
        # "read_timeout" | ("status", N) | "jsonrpc_error"; None = healthy.
        self.fail_next: str | tuple[str, int] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next is not None:
            how = self.fail_next
            self.fail_next = None
            if how == "connect":
                raise httpx.ConnectError("connection refused")
            if how == "connect_timeout":
                raise httpx.ConnectTimeout("timed out connecting")
            if how == "read_timeout":
                raise httpx.ReadTimeout("timed out reading")
            if how == "jsonrpc_error":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=(
                        b'{"jsonrpc": "2.0", "id": 1, '
                        b'"error": {"code": -32603, "message": "tool failed"}}'
                    ),
                )
            return httpx.Response(how[1], json={"error": "scripted"})
        return self.healthy(request)


async def _env(tmp_path, *, mock: FailingMCP, **breaker_kwargs):
    """One app with the breaker on (threshold 1 by default — the
    single-failure trips, the test-controlled case) and the failing
    mock wired."""
    async for ns in _make_env(
        tmp_path,
        config=make_config(mcp=_mcp_config(**breaker_kwargs)),
        mcp_mock=mock,
    ):
        yield ns


async def _key(env) -> str:
    return (await create_key(env.client, "brk-key"))["key"]


async def _breaker_state(env) -> dict:
    status = (
        await env.client.get("/admin/mcp", headers=auth(ADMIN_KEY))
    ).json()
    return status["circuit_breaker"]["servers"][SERVER.name]


def _circuit_open_outcome() -> float:
    """The counter's current value for the refusal outcome."""
    return metrics.MCP_REQUESTS.labels(
        server=SERVER.name, method="initialize", outcome="circuit_open"
    )._value.get()


# ----------------------------------------------------------------------
# What trips the circuit — and what never does
# ----------------------------------------------------------------------


async def test_connect_error_trips_then_refuses_in_band(tmp_path):
    """One unreachable answer (threshold 1) opens the circuit; the next
    message is refused IN-BAND — HTTP 200, JSON-RPC -32004, the id
    echoed — and never reaches the upstream (the request count froze:
    fail fast instead of burning another upstream timeout)."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        mock.fail_next = "connect"
        first = await _post(env.client, _init_body(), key)
        # The documented split: talking-to-the-server is the gateway's
        # concern — the 502 envelope, not a JSON-RPC error.
        assert first.status_code == 502, first.text

        before = len(mock.requests)
        second = await _post(env.client, _init_body(message_id=7), key)
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["id"] == 7
        assert body["error"]["code"] == CIRCUIT_OPEN
        assert SERVER.name in body["error"]["message"]
        assert len(mock.requests) == before  # nothing went upstream

        state = await _breaker_state(env)
        assert state["state"] == "open"
        assert state["opened_count"] == 1
        break


async def test_connect_timeout_is_a_connect_failure(tmp_path):
    """A connect timeout (distinct from a read timeout: the connection
    never established) is the same unreachable evidence."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        mock.fail_next = "connect_timeout"
        assert (await _post(env.client, _init_body(), key)).status_code == 502
        refused = await _post(env.client, _init_body(), key)
        assert refused.status_code == 200
        assert refused.json()["error"]["code"] == CIRCUIT_OPEN
        break


async def test_5xx_answer_trips(tmp_path):
    """An HTTP 500 answer is up-and-broken evidence: it relays verbatim
    (the client's JSON-RPC layer sees the server's own transport
    error), and the NEXT message is refused in-band."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        mock.fail_next = ("status", 500)
        first = await _post(env.client, _init_body(), key)
        assert first.status_code == 500  # relayed, not translated
        assert first.json() == {"error": "scripted"}

        refused = await _post(env.client, _init_body(), key)
        assert refused.status_code == 200
        assert refused.json()["error"]["code"] == CIRCUIT_OPEN
        break


async def test_read_timeout_never_trips(tmp_path):
    """Latency is not evidence — a tool server that accepts and then
    stalls gets the 502 envelope every time, but the circuit never
    opens: every message still goes upstream."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        for _ in range(4):  # threshold is 1 — four would-be trips
            mock.fail_next = "read_timeout"
            resp = await _post(env.client, _init_body(), key)
            assert resp.status_code == 502, resp.text
        assert (await _breaker_state(env))["state"] == "closed"
        assert len(mock.requests) == 4  # every one really went upstream
        break


async def test_4xx_and_jsonrpc_errors_are_evidence_of_health(tmp_path):
    """A 4xx answer and a JSON-RPC error frame in a 200 body are the
    server answering FINE (client-caused / protocol — the same rule
    the deployment breaker lives by): neither trips, and each resets
    the consecutive count. Proven the loud way: a 4xx between two
    would-be trips keeps the threshold-2 circuit closed forever."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock, failure_threshold=2):
        key = await _key(env)
        for _ in range(3):  # failure, health, failure, health, ...
            mock.fail_next = ("status", 500)
            assert (await _post(env.client, _init_body(), key)).status_code == 500
            mock.fail_next = ("status", 400)  # 4xx: the server is alive
            assert (await _post(env.client, _init_body(), key)).status_code == 400
        state = await _breaker_state(env)
        assert state["state"] == "closed"
        assert state["consecutive_failures"] == 0

        # A JSON-RPC error frame in a 200 body: the server spoke
        # protocol — health, not evidence of breakage.
        mock.fail_next = "jsonrpc_error"
        resp = await _post(env.client, _init_body(), key)
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32603
        state = await _breaker_state(env)
        assert state["state"] == "closed"
        assert state["consecutive_failures"] == 0
        break


# ----------------------------------------------------------------------
# Recovery: half-open, the probe, the admin escape hatch
# ----------------------------------------------------------------------


async def test_cooldown_half_open_probe_closes_on_success(tmp_path):
    """Cooldown 0: the open circuit half-opens immediately and the
    next message IS the probe — initialize is the natural one. One
    success closes the circuit; traffic flows again."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock, cooldown_seconds=0.0):
        key = await _key(env)
        mock.fail_next = "connect"
        assert (await _post(env.client, _init_body(), key)).status_code == 502
        # Cooldown 0: the effective state is already HALF_OPEN (the
        # snapshot reports what the NEXT request would see).
        assert (await _breaker_state(env))["state"] == "half_open"

        probe = await _post(env.client, _init_body(), key)  # the server healed
        assert probe.status_code == 200, probe.text
        assert probe.json()["result"]["serverInfo"]["name"] == "mock-mcp"
        state = await _breaker_state(env)
        assert state["state"] == "closed"
        assert state["opened_count"] == 1  # the trip stays on the record

        after = await _post(env.client, _init_body(), key)
        assert after.status_code == 200
        break


async def test_admin_reset_force_closes(tmp_path):
    """POST /admin/mcp/breakers/{server}/reset force-closes an open
    circuit (the operator fixed the server) — the next message probes
    immediately. An unknown server 404s."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        mock.fail_next = ("status", 503)
        assert (await _post(env.client, _init_body(), key)).status_code == 503
        assert (await _breaker_state(env))["state"] == "open"

        resp = await env.client.post(
            f"/admin/mcp/breakers/{SERVER.name}/reset", headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["server"] == SERVER.name
        assert body["breaker"]["state"] == "closed"

        after = await _post(env.client, _init_body(), key)
        assert after.status_code == 200  # went upstream — probe admitted

        missing = await env.client.post(
            "/admin/mcp/breakers/no-such-server/reset", headers=auth(ADMIN_KEY)
        )
        assert missing.status_code == 404
        break


async def test_admin_mcp_reports_the_breaker_block(tmp_path):
    """GET /admin/mcp carries the circuit_breaker block (enabled +
    per-server snapshots) and each server row its own snapshot — the
    same shape the deployment breakers report on /admin/routing."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        resp = await env.client.get("/admin/mcp", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200
        body = resp.json()
        assert body["circuit_breaker"]["enabled"] is True
        snap = body["circuit_breaker"]["servers"][SERVER.name]
        assert snap["state"] == "closed"
        server_row = next(s for s in body["servers"] if s["name"] == SERVER.name)
        assert server_row["breaker"] == snap
        break


async def test_readyz_reports_open_mcp_circuits_informationally(tmp_path):
    """An open tool-server circuit appears in /readyz's counts but
    never fails readiness — killing the pod would take the recovery
    (the half-open probe) away with it."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        mock.fail_next = "connect"
        assert (await _post(env.client, _init_body(), key)).status_code == 502
        env.app.state.ready = True
        ready = await env.client.get("/readyz")
        assert ready.status_code == 200
        body = ready.json()
        assert body["circuit_breakers"]["mcp"]["open"] == 1
        assert body["circuit_breakers"]["mcp"]["half_open"] == 0
        # The deployment breakers are untouched by MCP traffic.
        assert body["circuit_breakers"]["open"] == 0
        break


# ----------------------------------------------------------------------
# Disabled, and the accounting the refusal leaves behind
# ----------------------------------------------------------------------


async def test_disabled_breaker_never_blocks(tmp_path):
    """mcp.breaker.enabled: false — the state machine is off; every
    message goes upstream no matter how many connect errors pile up."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock, enabled=False):
        key = await _key(env)
        for _ in range(4):
            mock.fail_next = "connect"
            resp = await _post(env.client, _init_body(), key)
            assert resp.status_code == 502
        assert len(mock.requests) == 4
        state = await _breaker_state(env)
        assert state["state"] == "closed"
        break


async def test_refusal_accounting_row_and_metric(tmp_path):
    """A refusal writes the row as status=error (the client experienced
    a failure — the route-scoped SLO error-rate must count it) with the
    reason, and counts the request metric's outcome as the honest
    circuit_open — neither a server error nor a block."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        mock.fail_next = "connect"
        assert (await _post(env.client, _init_body(), key)).status_code == 502
        before = _circuit_open_outcome()
        refused = await _post(env.client, _init_body(), key)
        assert refused.status_code == 200
        assert _circuit_open_outcome() == before + 1

        logs = (
            await env.client.get(
                "/admin/logs", params={"limit": 5}, headers=auth(ADMIN_KEY)
            )
        ).json()["data"]
        refused_row = next(
            r
            for r in logs
            if r.get("status") == "error"
            and (r.get("error_message") or "") == "circuit breaker open"
        )
        assert refused_row["model"] == SERVER.name
        assert refused_row["route"] == "mcp"
        assert refused_row["provider"] == "mcp"
        # The unreachable attempt is on the record too, with its own
        # reason — the two errors stay distinguishable in the logs.
        unreachable = next(
            r
            for r in logs
            if r.get("status") == "error"
            and "unreachable" in (r.get("error_message") or "")
        )
        assert unreachable["model"] == SERVER.name
        break


async def test_gauge_family_shared_with_deployments(tmp_path):
    """The per-server breaker wires into the SAME gauge family the
    deployment breakers use — provider "mcp" keeps the series apart —
    so one dashboard panel shows every circuit in the fleet."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = await _key(env)
        mock.fail_next = "connect"
        assert (await _post(env.client, _init_body(), key)).status_code == 502
        assert (
            metrics.CIRCUIT_STATE.labels(
                deployment=SERVER.name, provider="mcp"
            )._value.get()
            == 2  # CircuitState.OPEN
        )
        assert (
            metrics.CIRCUIT_TRANSITIONS.labels(
                deployment=SERVER.name, provider="mcp", state="open"
            )._value.get()
            >= 1
        )
        break


# ----------------------------------------------------------------------
# The gate's position: before the rate limit and the concurrency slot
# ----------------------------------------------------------------------


async def test_open_circuit_consumes_no_rate_limit_budget(tmp_path):
    """The refusal happens BEFORE the rate limiter: a key with a
    1-request/minute budget, already spent, still gets the in-band
    refusal (not a 429) — an open circuit's answers are free, like
    every other pre-dispatch rejection."""
    mock = FailingMCP()
    async for env in _env(tmp_path, mock=mock):
        key = (await create_key(env.client, "tight-key", rpm_limit=1))["key"]
        mock.fail_next = "connect"
        assert (await _post(env.client, _init_body(), key)).status_code == 502
        # The RPM budget went on the request that really went upstream;
        # the refusal is free.
        second = await _post(env.client, _init_body(), key)
        assert second.status_code == 200
        assert second.json()["error"]["code"] == CIRCUIT_OPEN
        break


# ----------------------------------------------------------------------
# Shared state: the Redis backend under its own keyspace
# ----------------------------------------------------------------------


async def test_redis_backend_shares_mcp_trips_across_replicas(tmp_path):
    """mcp.breaker.backend: redis — one replica's trip opens the
    circuit for the whole fleet, in the MCP registry's OWN keyspace
    (a deployment happening to share the server's name keeps separate
    state); the admin reset closes it everywhere."""
    from fakeredis import FakeAsyncRedis

    from gateway.config import RoutingConfig
    from gateway.core.redis_backends import RedisCircuitBreakerRegistry

    redis = FakeAsyncRedis()
    mock = FailingMCP()
    config = make_config(
        mcp=MCPConfig(
            enabled=True,
            servers=[SERVER],
            breaker=BreakerConfig(
                backend="redis", failure_threshold=1, cooldown_seconds=30.0
            ),
        ),
    )
    async for env in _make_env(
        tmp_path, config=config, mcp_mock=mock, redis_client=redis
    ):
        # The wiring under test: the pipeline got the Redis registry.
        assert isinstance(
            env.app.state.pipeline.mcp_breakers, RedisCircuitBreakerRegistry
        )
        key = await _key(env)
        mock.fail_next = "connect"
        assert (await _post(env.client, _init_body(), key)).status_code == 502
        assert (await _breaker_state(env))["state"] == "open"  # read via Redis

        # Fleet-shared: a second replica's registry over the same
        # Redis sees the open circuit — while a DEPLOYMENT registry
        # of the same name (different keyspace) never does.
        mcp2 = RedisCircuitBreakerRegistry(
            redis,
            BreakerConfig(failure_threshold=1, cooldown_seconds=30.0),
            prefix=config.redis.prefix,
            namespace="mcp-cb",
        )
        mcp2.register(SERVER.name, "mcp")
        assert await mcp2.allow(SERVER.name) is False

        dep = RedisCircuitBreakerRegistry(
            redis, RoutingConfig().breaker, prefix=config.redis.prefix
        )
        dep.register(SERVER.name, "openai")
        assert await dep.allow(SERVER.name) is True

        # Admin reset closes it everywhere.
        resp = await env.client.post(
            f"/admin/mcp/breakers/{SERVER.name}/reset", headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200
        assert await mcp2.allow(SERVER.name) is True
        break


async def test_redis_keyspaces_keep_namespaces_apart():
    """Unit: the deployment keyspace (cb) and the MCP keyspace
    (mcp-cb) hash different keys for the same name — a trip in one is
    invisible to the other. The default (cb) keys are byte-identical
    to the pre-tier-66 shape, so an upgraded fleet never loses state."""
    from fakeredis import FakeAsyncRedis

    from gateway.core.redis_backends import RedisCircuitBreakerRegistry

    def clock() -> float:
        return 1_000_000.0

    redis = FakeAsyncRedis()
    dep = RedisCircuitBreakerRegistry(
        redis,
        BreakerConfig(failure_threshold=1, cooldown_seconds=30.0),
        prefix="t",
        clock=clock,
    )
    mcp = RedisCircuitBreakerRegistry(
        redis,
        BreakerConfig(failure_threshold=1, cooldown_seconds=30.0),
        prefix="t",
        clock=clock,
        namespace="mcp-cb",
    )
    dep.register("same-name", "openai")
    mcp.register("same-name", "mcp")
    await dep.record_failure("same-name")
    assert await dep.allow("same-name") is False
    assert await mcp.allow("same-name") is True  # the trip never leaked
    assert await redis.exists("t:cb:same-name")
    assert not await redis.exists("t:mcp-cb:same-name")
    await mcp.record_failure("same-name")  # mcp's own counter, from 0
    assert await mcp.allow("same-name") is False
    assert await redis.exists("t:mcp-cb:same-name")
