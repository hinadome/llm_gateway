"""Concurrency admission control: per-key/tenant in-flight caps with
priority queueing (QoS).

RPM/TPM bound the *rate* of requests; these caps bound how many may be
in-flight against the upstream at once. The slot wraps the upstream
call: acquired after the cache miss (hits and idempotent replays
never hold one), released where the in-flight gauge decrements — the
non-stream tail, the stream wrapper's finally and every error path, so
a slow upstream call holds the slot for exactly as long as it runs and
an error never leaks one. At the cap, requests QUEUE for up to
concurrency.wait_seconds, admitted highest-priority-first (FIFO
within a priority); an expired wait answers 429 + Retry-After. All
state is in-process, like the idempotency in-flight map: per-replica
shares.

Holding a slot from the outside: ``env.mock.delay_seconds`` makes the
upstream call itself slow (the slot is held for exactly the duration
of the call it wraps — the honest way to simulate a busy upstream).
"""

from __future__ import annotations

import asyncio

from prometheus_client.registry import REGISTRY

from gateway.config import ConcurrencyConfig

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _create_tenant(client, name: str, **fields) -> dict:
    resp = await client.post(
        "/admin/tenants", json={"name": name, **fields}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _post_chat(env, key: str, content: str = "hello", **extra):
    return await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=content, **extra),
        headers=auth(key),
    )


# ----------------------------------------------------------------------
# The cap: held slots, queueing, timeout
# ----------------------------------------------------------------------


async def test_second_request_queues_until_slot_frees(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=5.0))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "holder", max_concurrency=1))["key"]

        env.mock.delay_seconds = 1.0  # the upstream call holds the slot ~1s
        holder = asyncio.create_task(_post_chat(env, key, "slow upstream"))
        await asyncio.sleep(0.15)
        second = asyncio.create_task(_post_chat(env, key, "waiting for a slot"))
        await asyncio.sleep(0.15)

        # Mid-hold: one slot in flight, one waiter queued.
        assert _sample("gateway_concurrency_inflight", {"key": "holder"}) == 1.0
        assert _sample("gateway_concurrency_queued", {"key": "holder"}) == 1.0

        resp = await holder
        assert resp.status_code == 200
        resp = await second  # admitted the moment the holder's slot freed
        assert resp.status_code == 200

        env.mock.delay_seconds = 0.0
        assert _sample("gateway_concurrency_inflight", {"key": "holder"}) == 0.0
        assert _sample("gateway_concurrency_queued", {"key": "holder"}) == 0.0


async def test_queue_wait_timeout_429(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.25))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "tiny", max_concurrency=1))["key"]

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "slow upstream"))
        await asyncio.sleep(0.15)
        rejected_before = _sample(
            "gateway_concurrency_rejected_total", {"key": "tiny"}
        )

        second = await _post_chat(env, key, "no patience")
        assert second.status_code == 429
        err = second.json()["error"]
        assert err["code"] == "rate_limit_exceeded"
        assert "max 1 in-flight request" in err["message"]  # singular, cap=1
        assert second.headers["retry-after"] == "2"  # int(1.0) + 1
        assert (
            _sample("gateway_concurrency_rejected_total", {"key": "tiny"})
            - rejected_before
        ) == 1.0

        resp = await holder
        assert resp.status_code == 200
        env.mock.delay_seconds = 0.0

        # The waiters drained: the next request serves normally.
        ok = await _post_chat(env, key, "after the dust settles")
        assert ok.status_code == 200


async def test_fail_fast_when_wait_is_zero(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.0))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "fast-fail", max_concurrency=2))["key"]

        env.mock.delay_seconds = 1.0
        holders = [
            asyncio.create_task(_post_chat(env, key, f"held {i}")) for i in range(2)
        ]
        await asyncio.sleep(0.15)
        third = await _post_chat(env, key, "immediately refused")
        assert third.status_code == 429
        assert "max 2 in-flight requests" in third.json()["error"]["message"]
        for task in holders:
            assert (await task).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_capless_keys_skip_admission_entirely(tmp_path):
    # No cap on the key (and no tenant) -> no admission control, no
    # queueing, no gauges: parallel requests all proceed.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.1))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "free"))["key"]
        env.mock.delay_seconds = 0.5
        both = await asyncio.gather(
            _post_chat(env, key, "one"),
            _post_chat(env, key, "two"),
        )
        assert [r.status_code for r in both] == [200, 200]
        env.mock.delay_seconds = 0.0
        # No gauge series ever appeared for this key.
        assert _sample("gateway_concurrency_inflight", {"key": "free"}) == 0.0


async def test_disabled_via_config(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(enabled=False, wait_seconds=0.0))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "capped-but-off", max_concurrency=1))["key"]
        env.mock.delay_seconds = 0.5
        both = await asyncio.gather(
            _post_chat(env, key, "one"),
            _post_chat(env, key, "two"),
        )
        assert [r.status_code for r in both] == [200, 200]
        env.mock.delay_seconds = 0.0


# ----------------------------------------------------------------------
# Tenant pooled caps + QoS priority
# ----------------------------------------------------------------------


async def test_tenant_pooled_cap_blocks_member_keys(tmp_path):
    # The cap is the TENANT's: two different member keys contend for
    # one pooled slot, exactly like the pooled RPM window.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.25))
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", max_concurrency=1)
        k1 = (await create_key(env.client, "member-1", tenant_id=acme["id"]))["key"]
        k2 = (await create_key(env.client, "member-2", tenant_id=acme["id"]))["key"]

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, k1, "member 1 is busy"))
        await asyncio.sleep(0.15)
        second = await _post_chat(env, k2, "other key, same pool")
        assert second.status_code == 429
        assert "max 1 in-flight request" in second.json()["error"]["message"]
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_priority_admits_highest_first(tmp_path):
    # QoS: when waiters contend for the pooled tenant cap, the higher
    # priority key is admitted first — priority is a key attribute, so
    # it matters exactly where contention crosses keys.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=5.0))
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", max_concurrency=1)
        holder_k = (
            await create_key(env.client, "holder", tenant_id=acme["id"])
        )["key"]
        low_k = (
            await create_key(env.client, "low", tenant_id=acme["id"], priority=-5)
        )["key"]
        high_k = (
            await create_key(env.client, "high", tenant_id=acme["id"], priority=10)
        )["key"]

        done: list[str] = []

        async def _track(key: str, tag: str) -> None:
            resp = await _post_chat(env, key, f"request from {tag}")
            assert resp.status_code == 200, resp.text
            done.append(tag)

        env.mock.delay_seconds = 0.6
        holder = asyncio.create_task(_track(holder_k, "holder"))
        await asyncio.sleep(0.1)  # the low-priority waiter queues first
        low = asyncio.create_task(_track(low_k, "low"))
        await asyncio.sleep(0.1)  # then the high-priority one
        high = asyncio.create_task(_track(high_k, "high"))

        await asyncio.gather(holder, low, high)
        env.mock.delay_seconds = 0.0
        # The holder finished first (it started first, uncontended);
        # when its slot freed, `high` jumped the queue ahead of `low`.
        assert done[0] == "holder"
        assert done[1:] == ["high", "low"]


# ----------------------------------------------------------------------
# Slot lifetime: what holds it, what bypasses it, error paths
# ----------------------------------------------------------------------


async def test_cache_hits_bypass_admission(tmp_path):
    # Cache hits never touch the upstream, so they never need a slot:
    # a repeat request serves from cache while the key's only slot is
    # held by a slow upstream call.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.1))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "cached", max_concurrency=1))["key"]

        first = await _post_chat(env, key, "cached content")
        assert first.status_code == 200

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "different content"))
        await asyncio.sleep(0.15)
        repeat = await _post_chat(env, key, "cached content")
        assert repeat.status_code == 200  # would 429 if it needed a slot
        assert repeat.headers["x-gateway-cache"] == "HIT"
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_idempotent_replays_bypass_admission(tmp_path):
    # Same story for idempotency replays: the stored response serves
    # without a slot (phase 2b runs before 5.5).
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.1))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "careful", max_concurrency=1))["key"]

        original = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="executed once"),
            headers={**auth(key), "Idempotency-Key": "retry-1"},
        )
        assert original.status_code == 200

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "holds the only slot"))
        await asyncio.sleep(0.15)
        replay = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="executed once"),
            headers={**auth(key), "Idempotency-Key": "retry-1"},
        )
        assert replay.status_code == 200
        assert replay.headers["x-gateway-cache"] == "IDEMPOTENT"
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_slot_released_when_upstream_fails(tmp_path):
    # Every attempt fails -> retries exhausted -> the request errors
    # AND releases its slot. Proof in two parts: the per-key gauge is
    # back to 0 the moment the 5xx lands, and once the upstream
    # recovers (the all-open breaker bypass still routes) the next
    # request is ADMITTED immediately — a leaked slot would have
    # queued it into a 429 instead.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.2))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "unlucky", max_concurrency=1))["key"]
        env.mock.fail_counts["model-a"] = 10
        env.mock.fail_counts["model-b"] = 10

        boom = await _post_chat(env, key, "everything fails")
        assert boom.status_code >= 500

        # The slot came back with the error — the limiter's own gauge
        # says so (the response is only in hand after the error path's
        # _finish_span released it).
        assert _sample("gateway_concurrency_inflight", {"key": "unlucky"}) == 0.0

        # Upstream recovers; the breaker-open bypass routes there.
        env.mock.fail_counts["model-a"] = 0
        env.mock.fail_counts["model-b"] = 0
        ok = await _post_chat(env, key, "clean again")
        assert ok.status_code == 200  # admitted, not queue-blocked


async def test_streamed_response_releases_slot_on_completion(tmp_path):
    # Streams release in the stream wrapper's finally (the non-stream
    # path would never run there): a cap-1 key can stream, then
    # immediately serve the next request — the slot is not leaked.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.2))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "streamer", max_concurrency=1))["key"]

        streamed = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="streamed", stream=True),
            headers=auth(key),
        )
        assert streamed.status_code == 200
        await streamed.aread()

        after = await _post_chat(env, key, "next request needs the freed slot")
        assert after.status_code == 200


async def test_patch_applies_to_next_request(tmp_path):
    # The cap snapshots at auth time like every other ceiling: setting
    # it via PATCH applies from the NEXT request, never mid-flight.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.25))
    async for env in _make_env(tmp_path, config=config):
        key_info = await create_key(env.client, "later")
        key = key_info["key"]
        assert key_info["max_concurrency"] is None
        assert key_info["priority"] == 0

        first = await _post_chat(env, key, "before the cap exists")
        assert first.status_code == 200

        patched = await env.client.patch(
            f"/admin/keys/{key_info['id']}",
            json={"max_concurrency": 1},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["max_concurrency"] == 1

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "capped now"))
        await asyncio.sleep(0.15)
        second = await _post_chat(env, key, "blocked by the new cap")
        assert second.status_code == 429
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


# ----------------------------------------------------------------------
# Admin surface + events
# ----------------------------------------------------------------------


async def test_admin_concurrency_view(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=5.0))
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", max_concurrency=1)
        key_info = await create_key(
            env.client, "watched", max_concurrency=1, tenant_id=acme["id"]
        )
        key = key_info["key"]

        env.mock.delay_seconds = 1.5
        holder = asyncio.create_task(_post_chat(env, key, "in flight"))
        await asyncio.sleep(0.1)
        queued = asyncio.create_task(_post_chat(env, key, "in queue"))
        await asyncio.sleep(0.1)

        view = (
            await env.client.get("/admin/concurrency", headers=auth(ADMIN_KEY))
        ).json()
        assert view["enabled"] is True
        assert view["wait_seconds"] == 5.0
        assert view["queued_total"] == 1
        entry = next(e for e in view["keys"] if e["key_id"] == key_info["id"])
        assert entry["key_name"] == "watched"  # id -> name join
        assert entry["inflight"] == 1
        assert entry["queued"] == 1
        assert view["tenants"] == [
            {"tenant_id": acme["id"], "inflight": 1, "tenant_name": "acme"}
        ]

        assert (await holder).status_code == 200
        assert (await queued).status_code == 200
        env.mock.delay_seconds = 0.0

        # Tenant admins never see the platform view.
        tadmin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        denied = await env.client.get(
            "/admin/concurrency", headers=auth(tadmin["key"])
        )
        assert denied.status_code == 403


async def test_concurrency_timeout_event(tmp_path):
    from .test_events import Recorder  # local import: no circular module dep

    from gateway.config import EventHandlerConfig, EventsConfig

    config = make_config(
        concurrency=ConcurrencyConfig(wait_seconds=0.25),
        events=EventsConfig(
            handlers=[
                EventHandlerConfig(
                    class_path="tests.test_events:Recorder",
                    events=["concurrency.timeout"],
                )
            ]
        ),
    )
    async for env in _make_env(tmp_path, config=config):
        recorder = next(
            h for h in env.app.state.events.handlers if isinstance(h, Recorder)
        )
        key = (await create_key(env.client, "alarmed", max_concurrency=1))["key"]

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "slow upstream"))
        await asyncio.sleep(0.15)
        second = await _post_chat(env, key, "this wait will expire")
        assert second.status_code == 429
        await env.app.state.events.drain()

        assert [e.name for e in recorder.events] == ["concurrency.timeout"]
        payload = recorder.events[0].payload
        assert payload["key_name"] == "alarmed"
        assert payload["model"] == "test-model"
        assert payload["max_concurrency"] == 1
        assert payload["wait_seconds"] == 0.25
        assert payload["waited_seconds"] >= 0.25

        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_api_surface_and_validation(tmp_path):
    async for env in _make_env(tmp_path):
        # Key surface: cap + priority on create, echo on the row,
        # PATCH moves both, explicit null resets the cap (and priority
        # null means the default class 0).
        created = await create_key(
            env.client, "gold", max_concurrency=5, priority=7
        )
        assert created["max_concurrency"] == 5
        assert created["priority"] == 7

        patched = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"max_concurrency": None, "priority": None},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["max_concurrency"] is None
        assert patched.json()["priority"] == 0

        # Validation: caps are positive ints, priority bounded. Body
        # validation surfaces as the OpenAI-style 400.
        for bad in ({"max_concurrency": 0}, {"priority": 1001}):
            resp = await env.client.post(
                "/admin/keys",
                json={"name": "bad", **bad},
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 400, bad

        # Tenant surface: the pooled cap, same shape.
        tenant = await _create_tenant(env.client, "acme", max_concurrency=8)
        assert tenant["max_concurrency"] == 8
        patched_tenant = await env.client.patch(
            f"/admin/tenants/{tenant['id']}",
            json={"max_concurrency": None},
            headers=auth(ADMIN_KEY),
        )
        assert patched_tenant.status_code == 200
        assert patched_tenant.json()["max_concurrency"] is None
