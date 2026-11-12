"""Tests for shadow deployments (mirrored traffic for evaluation).

Shadow deployments never serve client traffic — the router excludes them
from selection. After a *successful* primary response, a fraction of
requests (``shadow_fraction``) is mirrored to them in the background via
the non-streaming ``complete()`` path (even for streamed requests, so
mirrored calls stay comparable). Mirrors never affect the client: no
guardrails, no rate limits, no budgets, no cache; results land in the
shadow metrics, the deployment's circuit breaker, and shadow request-log
rows (own cost/bodies, excluded from /admin/stats and key spend).

Canary-style rollouts need no separate mechanism: ``weight`` within a
same-priority tier is weighted-random load balancing, so raising a
deployment's weight gradually shifts real traffic to it.
"""

from __future__ import annotations

import asyncio

from gateway.config import DeploymentConfig, GatewayConfig, ModelConfig
from gateway.errors import ProviderError

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _wait_for
from .test_budget_windows import _field_check, _fresh, _key_id
from .test_pipeline import read_sse


def _shadow_config(shadow_extra: dict | None = None) -> GatewayConfig:
    """One serving deployment + one shadow twin on the same model."""
    extra = {"shadow": True}
    extra.update(shadow_extra or {})
    return make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/primary", provider="openai", model="model-a"
                    ),
                    DeploymentConfig(
                        name="openai/shadow",
                        provider="openai",
                        model="model-shadow",
                        **extra,
                    ),
                ],
            )
        ],
        pricing={
            "model-a": {"input": 2.0, "output": 4.0},
            "model-shadow": {"input": 2.0, "output": 4.0},
        },
    )


async def _log_rows(client, **params) -> list[dict]:
    resp = await client.get("/admin/logs", params=params, headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ----------------------------------------------------------------------
# Units: routing exclusion + config
# ----------------------------------------------------------------------


def test_shadow_fraction_is_clamped():
    high = DeploymentConfig(
        name="d", provider="openai", model="m", shadow=True, shadow_fraction=7.5
    )
    low = DeploymentConfig(
        name="d", provider="openai", model="m", shadow=True, shadow_fraction=-3.0
    )
    assert high.shadow_fraction == 1.0
    assert low.shadow_fraction == 0.0


def test_router_excludes_shadow_from_serving():
    from gateway.core.router import Router

    config = _shadow_config()
    router = Router(config, {})
    assert [d.name for d in router.resolve("test-model")] == ["openai/primary"]
    assert [d.name for d in router.shadow_deployments("test-model")] == ["openai/shadow"]
    # unknown models are still a 404-style miss
    from gateway.errors import ModelNotFoundError

    try:
        router.resolve("nope")
    except ModelNotFoundError:
        pass
    else:
        raise AssertionError("expected ModelNotFoundError")


def test_all_shadow_model_cannot_serve():
    from gateway.core.router import Router

    config = make_config(
        models=[
            ModelConfig(
                name="ghost-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/only-shadow",
                        provider="openai",
                        model="model-shadow",
                        shadow=True,
                    )
                ],
            )
        ]
    )
    router = Router(config, {})
    try:
        router.resolve("ghost-model")
    except ProviderError as exc:
        assert exc.status_code == 503
        assert "shadow" in exc.message
    else:
        raise AssertionError("expected 503 for a model with only shadow deployments")


# ----------------------------------------------------------------------
# e2e: mirroring behavior
# ----------------------------------------------------------------------


async def test_mirror_fires_on_success(tmp_path):
    async for env in _make_env(tmp_path, config=_shadow_config()):
        key = (await create_key(env.client, "shadow-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="mirror me"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-deployment"] == "openai/primary"
        request_id = resp.headers["x-request-id"]
        primary_cost = float(resp.headers["x-gateway-cost-usd"])
        assert primary_cost > 0

        # the mirror is a background task — poll for the upstream copy
        await _wait_for(
            lambda: len(env.mock.requests) >= 2, what="shadow mirror sent"
        )
        primary, mirror = env.mock.requests[-2], env.mock.requests[-1]
        assert primary["body"]["model"] == "model-a"
        assert mirror["body"]["model"] == "model-shadow"
        assert mirror["url"].endswith("/chat/completions")

        # shadow log row: same request_id, flagged, own cost — while the
        # key only pays for the primary
        async def _shadow_logged() -> bool:
            rows = await _log_rows(env.client, request_id=request_id)
            return (
                len(rows) == 2
                and sum(1 for r in rows if r["shadow"]) == 1
                and sum(1 for r in rows if not r["shadow"]) == 1
            )

        await _wait_for(_shadow_logged, what="shadow log row written")
        rows = await _log_rows(env.client, request_id=request_id)
        shadow_row = next(r for r in rows if r["shadow"])
        assert shadow_row["deployment"] == "openai/shadow"
        assert shadow_row["status"] == "success"
        assert shadow_row["cost_usd"] and shadow_row["cost_usd"] > 0
        assert shadow_row["stream"] is False

        key_id = await _key_id(env.client, "shadow-user")
        await _wait_for(
            _field_check(env, key_id, "spend_usd", primary_cost),
            what="primary spend visible",
        )
        await asyncio.sleep(0.25)  # any (wrong) shadow spend would land by now
        fresh = await _fresh(env, key_id)
        assert fresh["spend_usd"] == primary_cost  # mirror costs the key nothing

        # /admin/stats counts client traffic only (1 request, 1 cost)
        stats = (
            await env.client.get("/admin/stats", headers=auth(ADMIN_KEY))
        ).json()
        assert stats["total_requests"] == 1
        assert stats["tokens"] == {"prompt": 10, "completion": 7}
        assert abs(stats["cost_usd_total"] - primary_cost) < 1e-9
        break


async def test_mirror_skipped_on_cache_hit(tmp_path):
    async for env in _make_env(tmp_path, config=_shadow_config()):
        key = (await create_key(env.client, "cacheful"))["key"]
        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="serve me twice"),
            headers=auth(key),
        )
        assert first.status_code == 200
        await _wait_for(
            lambda: len(env.mock.requests) >= 2, what="first mirror sent"
        )

        replay = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="serve me twice"),
            headers=auth(key),
        )
        assert replay.headers["x-gateway-cache"] == "HIT"
        await asyncio.sleep(0.25)  # a (wrong) mirror would land by now
        assert len(env.mock.requests) == 2  # primary + first mirror only
        break


async def test_mirror_skipped_when_blocked(tmp_path):
    async for env in _make_env(tmp_path, config=_shadow_config()):
        key = (await create_key(env.client, "blocked-user"))["key"]
        # "BADOUTPUT" makes the mock return a response that the output
        # moderation guardrail blocks.
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="BADOUTPUT"),
            headers=auth(key),
        )
        assert resp.status_code == 200  # content_filter body, HTTP 200
        body = resp.json()
        assert body["choices"][0]["finish_reason"] == "content_filter"
        await asyncio.sleep(0.25)
        # the primary was called; the blocked response was not mirrored
        assert len(env.mock.requests) == 1
        rows = await _log_rows(env.client)
        assert not any(r["shadow"] for r in rows)
        break


async def test_zero_fraction_never_mirrors(tmp_path):
    async for env in _make_env(
        tmp_path, config=_shadow_config({"shadow_fraction": 0.0})
    ):
        key = (await create_key(env.client, "unmirrored"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="quiet please"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.25)
        assert len(env.mock.requests) == 1  # primary only
        break


async def test_streamed_request_mirrored_non_streaming(tmp_path):
    async for env in _make_env(tmp_path, config=_shadow_config()):
        key = (await create_key(env.client, "stream-shadow"))["key"]
        async with env.client.stream(
            "POST",
            "/v1/chat/completions",
            json=chat_body(
                content="stream and mirror",
                stream=True,
                stream_options={"include_usage": True},
            ),
            headers=auth(key),
        ) as resp:
            lines = await read_sse(resp)
        assert resp.headers["x-gateway-deployment"] == "openai/primary"
        assert lines[-1] == "[DONE]"

        await _wait_for(
            lambda: len(env.mock.requests) >= 2, what="mirror of stream sent"
        )
        mirror = env.mock.requests[-1]
        assert mirror["body"]["model"] == "model-shadow"
        assert mirror["body"]["stream"] is False  # always complete(), not stream()
        break


async def test_shadow_failure_recorded_not_client(tmp_path):
    async for env in _make_env(tmp_path, config=_shadow_config()):
        # the mirrored call fails once; the client must not notice
        env.mock.fail_counts["model-shadow"] = 1
        key = (await create_key(env.client, "resilient"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="failing shadow"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"]

        await _wait_for(
            lambda: len(env.mock.requests) >= 2, what="mirror attempted"
        )
        # the shadow's breaker recorded the request-level failure
        routing = (
            await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))
        ).json()
        shadow_breaker = next(
            d["breaker"]
            for m in routing["models"]
            for d in m["deployments"]
            if d["name"] == "openai/shadow"
        )
        assert shadow_breaker["consecutive_failures"] == 1
        assert shadow_breaker["state"] == "closed"  # below the threshold

        # ... and the shadow log row records the error outcome
        request_id = resp.headers["x-request-id"]

        async def _shadow_errored() -> bool:
            rows = await _log_rows(env.client, request_id=request_id)
            return any(r["shadow"] and r["status"] == "error" for r in rows)

        await _wait_for(_shadow_errored, what="shadow error row written")
        break


async def test_shadow_metrics_exposed(tmp_path):
    async for env in _make_env(tmp_path, config=_shadow_config()):
        key = (await create_key(env.client, "metered"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="measure me"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        await _wait_for(
            lambda: len(env.mock.requests) >= 2, what="mirror sent"
        )
        metrics_body = (await env.client.get("/metrics")).text
        assert "gateway_shadow_requests_total" in metrics_body
        assert "gateway_shadow_latency_seconds" in metrics_body
        break


async def test_routing_table_reports_shadow_fields(tmp_path):
    async for env in _make_env(
        tmp_path, config=_shadow_config({"shadow_fraction": 0.25})
    ):
        routing = (
            await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))
        ).json()
        deployment = next(
            d
            for m in routing["models"]
            for d in m["deployments"]
            if d["name"] == "openai/shadow"
        )
        assert deployment["shadow"] is True
        assert deployment["shadow_fraction"] == 0.25
        primary = next(
            d
            for m in routing["models"]
            for d in m["deployments"]
            if d["name"] == "openai/primary"
        )
        assert primary["shadow"] is False
        break
