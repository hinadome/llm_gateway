"""Runaway-agent protection: identical-request loop detection with an
escalating cooldown jail.

An agent stuck in a loop (retry storm, regenerating tool-call loop)
sends the SAME request over and over at a rate no legitimate caller
produces — every request individually legal, so RPM/TPM cannot see it.
What identifies the runaway is the *fingerprint* — the same normalized
identity the response cache keys on — repeated ``threshold`` times
inside the window. On trip the KEY is jailed: every request from it
answers 429 (+ Retry-After) until the cooldown expires — including
would-be cache hits and idempotent replays, because nothing a stuck
agent sends is useful. Re-trips escalate; a quiet period decays the
offense count; POST /admin/runaway/{key_id}/release is the admin
escape hatch. Admin/env-key traffic (no key_id) is exempt, like every
key-layer ceiling. The clock is injectable so cooldown/window/escalation
tests fast-forward instead of sleeping.
"""

from __future__ import annotations

from prometheus_client.registry import REGISTRY

from gateway.config import EventHandlerConfig, EventsConfig, RunawayConfig

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _config(**overrides) -> RunawayConfig:
    """Tight thresholds so trips happen in 3 requests, cooldowns the
    tests can assert precisely (Retry-After = int(seconds) + 1)."""
    overrides.setdefault("identical_request_threshold", 3)
    return RunawayConfig(**overrides)


def _fake_clock(env) -> list:
    """Inject a controllable clock into the protector (monotonic
    seconds in a one-cell list) and return the cell."""
    t = [1000.0]
    env.app.state.pipeline.runaway._clock = lambda: t[0]
    return t


async def _trip(client, key: str, content: str = "loop me", count: int = 3):
    """Send ``count`` identical requests; the last one trips."""
    for i in range(count):
        resp = await client.post(
            "/v1/chat/completions",
            json=chat_body(content=content),
            headers=auth(key),
        )
    return resp


# ----------------------------------------------------------------------
# The loop detector
# ----------------------------------------------------------------------


async def test_identical_loop_trips_at_threshold(tmp_path):
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "loop-bot"))["key"]

        # 1st: served by the upstream (MISS).
        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stuck in a loop"),
            headers=auth(key),
        )
        assert first.status_code == 200
        assert first.headers["x-gateway-cache"] == "MISS"

        # 2nd: the response cache serves it free — and it still counts
        # toward the loop (the trip check runs BEFORE the cache
        # lookup; a free hit is still a stuck agent).
        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stuck in a loop"),
            headers=auth(key),
        )
        assert second.status_code == 200
        assert second.headers["x-gateway-cache"] == "HIT"

        # 3rd identical request inside the window: refused, key jailed.
        third = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stuck in a loop"),
            headers=auth(key),
        )
        assert third.status_code == 429
        err = third.json()["error"]
        assert err["code"] == "rate_limit_exceeded"
        assert "identical requests" in err["message"]
        # RFC 7231 Retry-After: int(cooldown 30s) + 1.
        assert third.headers["retry-after"] == "31"


async def test_distinct_requests_never_trip(tmp_path):
    # Different content = different fingerprint: a key making many
    # *varied* requests is load, not a loop — nothing trips.
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "busy-human"))["key"]
        for i in range(8):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content=f"question number {i}"),
                headers=auth(key),
            )
            assert resp.status_code == 200, i


async def test_jail_blocks_different_requests_too(tmp_path):
    # The jail is on the KEY, not the request: while in cooldown even
    # a genuinely different request 429s — the agent is stuck, so
    # nothing it sends is trusted until the cooldown expires.
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "loop-bot"))["key"]
        await _trip(env.client, key)
        different = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="a completely different question"),
            headers=auth(key),
        )
        assert different.status_code == 429
        assert different.json()["error"]["code"] == "rate_limit_exceeded"
        # Retry-After carries the REMAINING cooldown (just under the
        # 30s sentence here, ceiled per RFC 7231) — not the full one.
        assert different.headers["retry-after"] == "30"


async def test_window_expiry_forgets_old_hits(tmp_path):
    # Only hits inside the window count: 2 requests, a gap longer than
    # the window, 2 more — the old hits are pruned, no trip; the next
    # fresh identical request completes the *new* loop and trips.
    config = make_config(
        runaway=_config(identical_request_window_seconds=60.0)
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "bursty"))["key"]
        t = _fake_clock(env)

        for _ in range(2):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="burst"),
                headers=auth(key),
            )
            assert resp.status_code == 200

        t[0] += 61.0  # beyond the window — the first two hits decay

        for _ in range(2):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="burst"),
                headers=auth(key),
            )
            assert resp.status_code == 200

        fifth = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="burst"),
            headers=auth(key),
        )
        assert fifth.status_code == 429  # 3 fresh hits inside the window


async def test_disabled_via_config(tmp_path):
    # enabled: false = the check is a no-op: identical fan-out
    # (benchmarks) sails through untouched.
    config = make_config(runaway=_config(enabled=False))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "bencher"))["key"]
        for _ in range(5):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="identical fan-out"),
                headers=auth(key),
            )
            assert resp.status_code == 200


# ----------------------------------------------------------------------
# Cooldown jail: expiry, escalation, cross-surface behavior
# ----------------------------------------------------------------------


async def test_cooldown_expiry_and_single_retry_passes(tmp_path):
    # After the cooldown the jail lifts. The loop windows were CLEARED
    # on trip — the punishment is the cooldown, not a grudge — so a
    # human investigating with one retry passes, while a still-looping
    # agent re-trips on fresh hits (next test).
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "loop-bot"))["key"]
        t = _fake_clock(env)

        tripped = await _trip(env.client, key)
        assert tripped.status_code == 429

        t[0] += 30.1  # cooldown expired

        one = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stuck in a loop"),
            headers=auth(key),
        )
        assert one.status_code == 200  # single investigation retry passes

        two = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stuck in a loop"),
            headers=auth(key),
        )
        assert two.status_code == 200  # 2 fresh hits, below threshold


async def test_escalation_doubles_on_retrip(tmp_path):
    # A still-looping agent re-trips after the cooldown lifts and the
    # jail doubles: 30s -> 60s (Retry-After 31 -> 61), offenses 1 -> 2.
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "loop-bot"))["key"]
        t = _fake_clock(env)

        first = await _trip(env.client, key)
        assert first.status_code == 429
        assert first.headers["retry-after"] == "31"

        t[0] += 30.1  # first cooldown served

        second = await _trip(env.client, key)  # fresh loop, re-trips
        assert second.status_code == 429
        assert second.headers["retry-after"] == "61"

        view = (
            await env.client.get("/admin/runaway", headers=auth(ADMIN_KEY))
        ).json()
        assert view["jailed"][0]["offenses"] == 2


async def test_offense_decay_after_quiet_period(tmp_path):
    # A quiet reset_after_seconds with no trips decays the offense
    # count: the NEXT trip is a first offense again (30s, not 60s).
    config = make_config(runaway=_config(reset_after_seconds=120.0))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "reformed"))["key"]
        t = _fake_clock(env)

        first = await _trip(env.client, key)
        assert first.headers["retry-after"] == "31"

        t[0] += 121.0  # quiet period served, jail long expired
        second = await _trip(env.client, key)
        assert second.status_code == 429
        assert second.headers["retry-after"] == "31"  # first offense again


async def test_stream_and_nonstream_share_one_fingerprint(tmp_path):
    # stream is not part of the cache identity, so the same content
    # alternated between streamed and plain requests is ONE loop: the
    # third arrival trips regardless of its streaming flag.
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "streamer"))["key"]

        for stream in (True, False, True):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="same either way", stream=stream),
                headers=auth(key),
            )
            await resp.aread()
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "rate_limit_exceeded"


async def test_loop_across_routes_trips_as_one(tmp_path):
    # The fingerprint is the normalized request (messages + model +
    # knobs), identical whether it arrived via OpenAI chat, the
    # Responses API or the native routes: an agent looping across
    # surfaces is still one loop.
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "shape-shifter"))["key"]
        body = chat_body(content="hello")

        first = await env.client.post(
            "/v1/chat/completions", json=body, headers=auth(key)
        )
        assert first.status_code == 200

        second = await env.client.post(
            "/v1/responses",
            json={"model": "test-model", "input": "hello"},
            headers=auth(key),
        )
        assert second.status_code == 200

        third = await env.client.post(
            "/v1/chat/completions", json=body, headers=auth(key)
        )
        assert third.status_code == 429


async def test_jailed_idempotent_replay_also_refused(tmp_path):
    # The jail precedes idempotency (step 1.5 < 2b): a stuck agent
    # replaying its Idempotency-Key gets the 429, not the stored
    # response — replaying a loop is still looping.
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "careful-client"))["key"]

        stored = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="original, executed once"),
            headers={**auth(key), "Idempotency-Key": "retry-safe-1"},
        )
        assert stored.status_code == 200

        await _trip(env.client, key)  # the key ends up jailed

        replay = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="original, executed once"),
            headers={**auth(key), "Idempotency-Key": "retry-safe-1"},
        )
        assert replay.status_code == 429
        assert replay.json()["error"]["code"] == "rate_limit_exceeded"


# ----------------------------------------------------------------------
# Exemptions and observability
# ----------------------------------------------------------------------


async def test_platform_admin_traffic_exempt(tmp_path):
    # Admin/env-key traffic carries no key_id — the jail has nothing
    # to bind to — exactly like every other key-layer ceiling.
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        for _ in range(5):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="identical admin traffic"),
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 200


async def test_trip_event_and_metric(tmp_path):
    # Observer-only lifecycle event (an orchestration layer can alarm
    # or revoke the key on it) + the per-key trip counter.
    from .test_events import Recorder  # local import: no circular module dep

    config = make_config(
        runaway=_config(),
        events=EventsConfig(
            handlers=[
                EventHandlerConfig(
                    class_path="tests.test_events:Recorder", events=["runaway.trip"]
                )
            ]
        ),
    )
    async for env in _make_env(tmp_path, config=config):
        recorder = next(
            h
            for h in env.app.state.events.handlers
            if isinstance(h, Recorder)
        )
        key = (await create_key(env.client, "alarmed"))["key"]

        before = _sample("gateway_runaway_trips_total", {"key": "alarmed"})
        tripped = await _trip(env.client, key)
        assert tripped.status_code == 429
        await env.app.state.events.drain()

        assert [e.name for e in recorder.events] == ["runaway.trip"]
        payload = recorder.events[0].payload
        assert payload["key_name"] == "alarmed"
        assert payload["model"] == "test-model"
        assert payload["count"] == 3
        assert payload["threshold"] == 3
        assert payload["window_seconds"] == 60.0
        assert payload["cooldown_seconds"] == 30.0
        assert payload["offenses"] == 1

        assert (
            _sample("gateway_runaway_trips_total", {"key": "alarmed"}) - before
        ) == 1.0


# ----------------------------------------------------------------------
# Admin surface: jail view + release
# ----------------------------------------------------------------------


async def test_admin_view_and_release(tmp_path):
    config = make_config(runaway=_config())
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "jailed-key"))["key"]
        tripped = await _trip(env.client, key)
        assert tripped.status_code == 429

        view = (
            await env.client.get("/admin/runaway", headers=auth(ADMIN_KEY))
        ).json()
        assert view["enabled"] is True
        assert view["config"]["identical_request_threshold"] == 3
        assert len(view["jailed"]) == 1
        entry = view["jailed"][0]
        assert entry["key_name"] == "jailed-key"  # id -> name join
        assert entry["offenses"] == 1
        assert 0 < entry["retry_after_seconds"] <= 30.0

        # Release: the escape hatch. The key serves again immediately,
        # and a re-trip starts back at the FIRST-offense cooldown —
        # release clears the offense history, not just the jail.
        released = await env.client.post(
            f"/admin/runaway/{entry['key_id']}/release", headers=auth(ADMIN_KEY)
        )
        assert released.status_code == 200
        assert released.json() == {"key_id": entry["key_id"], "released": True}

        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stuck in a loop"),
            headers=auth(key),
        )
        assert ok.status_code == 200

        retrip = await _trip(env.client, key)
        assert retrip.status_code == 429
        assert retrip.headers["retry-after"] == "31"  # not escalated

        # Releasing a free key is a 404 (key_not_jailed), not a no-op.
        gone = await env.client.post(
            f"/admin/runaway/{entry['key_id']}/release", headers=auth(ADMIN_KEY)
        )
        # ...but the key IS jailed right now (re-tripped above); use a
        # key that was never jailed.
        fresh = await create_key(env.client, "never-jailed")
        never = await env.client.post(
            f"/admin/runaway/{fresh['id']}/release", headers=auth(ADMIN_KEY)
        )
        assert never.status_code == 404
        assert never.json()["error"]["code"] == "key_not_jailed"
