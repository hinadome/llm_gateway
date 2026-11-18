"""Synthetic canary probes: scheduled deployment health checks.

The contract: probes go straight to each deployment's provider
(direct adapter calls — no pipeline, so no request-log rows, no
spend, no breaker accounting); every configured deployment is probed
(shadows and fallbacks included — the deployments real traffic
rarely reaches are the ones whose health traffic cannot measure);
failures emit ``canary.failed`` and a return to health emits
``canary.recovered``; outcomes are ok / timeout / error; the admin
endpoints are platform-admin-only (deployment topology is platform
configuration, like /admin/routing). The lifespan loop never runs
under pytest — every test drives probe_once() or the probe-now
endpoint directly.
"""

from __future__ import annotations

import httpx

from gateway.config import (
    CanaryConfig,
    DeploymentConfig,
    EventsConfig,
    EventHandlerConfig,
    ModelConfig,
)
from gateway.observability.metrics import CANARY_PROBES
from gateway.providers.base import network_error

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_tenants import _create_tenant


def _record_config(**canary) -> "make_config":
    return make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        ),
        canary=CanaryConfig(**canary),
    )


async def _drain(app) -> list:
    await app.state.events.drain()
    recorder = app.state.events.handlers[0]
    events, recorder.events = recorder.events, []
    return events


async def test_canary_wave_probes_every_deployment(env):
    before = CANARY_PROBES.labels(
        deployment="openai/primary", provider="openai", outcome="ok"
    )._value.get()
    results = await env.app.state.canary.probe_once()
    # Every configured deployment — primary, fallback and the quiet
    # "other" model alike (the deployments traffic rarely reaches are
    # the point of the feature).
    assert [r["deployment"] for r in results] == [
        "openai/fallback",
        "openai/other",
        "openai/primary",
    ]
    assert all(r["outcome"] == "ok" and r["healthy"] for r in results)
    assert all(r["latency_ms"] is not None and r["checked_at"] for r in results)
    assert CANARY_PROBES.labels(
        deployment="openai/primary", provider="openai", outcome="ok"
    )._value.get() == before + 1

    # The probes went out with the configured shape: prompt + capped
    # output, one per deployment's *native* model (a/b/c).
    bodies = [r for r in env.mock.requests if "messages" in r["body"]]
    assert len(bodies) == 3
    assert {b["body"]["model"] for b in bodies} == {"model-a", "model-b", "model-c"}
    for b in bodies:
        assert b["body"]["max_tokens"] == 1
        assert b["body"]["messages"] == [{"role": "user", "content": "ping"}]

    # Direct adapter calls: no pipeline machinery ran — nothing in the
    # request log, nothing billed.
    logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
    assert logs.json()["data"] == []


async def test_canary_failure_outcome_metric_and_event(tmp_path):
    async for env in _make_env(tmp_path, config=_record_config()):
        env.mock.fail_counts["model-b"] = 2  # openai/fallback fails this wave
        results = await env.app.state.canary.probe_once()
        by_name = {r["deployment"]: r for r in results}
        failed = by_name["openai/fallback"]
        assert failed["outcome"] == "error"
        assert failed["healthy"] is False
        assert "upstream boom" in failed["error"]
        assert by_name["openai/primary"]["outcome"] == "ok"

        assert CANARY_PROBES.labels(
            deployment="openai/fallback", provider="openai", outcome="error"
        )._value.get() == 1

        events = await _drain(env.app)
        failed_events = [e for e in events if e.name == "canary.failed"]
        assert [e.payload["deployment"] for e in failed_events] == ["openai/fallback"]
        assert failed_events[0].payload["outcome"] == "error"
        assert "upstream boom" in failed_events[0].payload["error"]
        # Healthy deployments never emit.
        assert not [e for e in events if e.name == "canary.recovered"]


async def test_canary_recovery_event(tmp_path):
    async for env in _make_env(tmp_path, config=_record_config()):
        env.mock.fail_counts["model-b"] = 1
        prober = env.app.state.canary
        await prober.probe_once()  # fails (consumes the fail_count)
        first_wave = await _drain(env.app)
        assert [e.name for e in first_wave if e.name.startswith("canary.")] == [
            "canary.failed"
        ]

        await prober.probe_once()  # back to ok — recovered
        second_wave = await _drain(env.app)
        recovered = [e for e in second_wave if e.name == "canary.recovered"]
        assert [e.payload["deployment"] for e in recovered] == ["openai/fallback"]
        assert recovered[0].payload["latency_ms"] >= 0

        await prober.probe_once()  # still ok — no repeat transition
        third_wave = await _drain(env.app)
        assert not [e for e in third_wave if e.name.startswith("canary.")]


async def test_canary_timeout_classification(tmp_path):
    # The wire distinguishing timeout from error: a ProviderError
    # carrying an httpx timeout in its cause chain (exactly what
    # _read_json's network_error produces) reads as "timeout".
    async for env in _make_env(tmp_path, config=_record_config()):
        adapter = env.app.state.pipeline.router.providers["openai"]

        async def never_answers(deployment, request, provider_cfg, timeout):
            # Exactly what _read_json produces on a transport timeout:
            # network_error(...) raised `from` the httpx exception.
            timeout_exc = httpx.ReadTimeout("read timed out")
            raise network_error("openai", timeout_exc) from timeout_exc

        adapter.complete = never_answers
        results = await env.app.state.canary.probe_once()
        assert all(r["outcome"] == "timeout" for r in results)
        assert all(r["healthy"] is False for r in results)
        assert CANARY_PROBES.labels(
            deployment="openai/primary", provider="openai", outcome="timeout"
        )._value.get() == 1
        events = await _drain(env.app)
        failed = [e for e in events if e.name == "canary.failed"]
        assert {e.payload["outcome"] for e in failed} == {"timeout"}


async def test_canary_admin_endpoints(env):
    # Before any wave: the whole fleet listed, outcomes unknown.
    status = await env.client.get("/admin/canaries", headers=auth(ADMIN_KEY))
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is False  # disabled by default...
    assert [r["deployment"] for r in body["data"]] == [
        "openai/fallback",
        "openai/other",
        "openai/primary",
    ]
    assert all(r["outcome"] == "unknown" and r["checked_at"] is None for r in body["data"])

    # ...but a manual wave still runs on demand.
    probed = await env.client.post("/admin/canaries/probe", headers=auth(ADMIN_KEY))
    assert probed.status_code == 200
    probe_body = probed.json()
    assert probe_body["probed"] == 3
    assert probe_body["healthy"] == 3
    assert all(r["outcome"] == "ok" for r in probe_body["data"])

    status = await env.client.get("/admin/canaries", headers=auth(ADMIN_KEY))
    assert all(r["outcome"] == "ok" and r["checked_at"] for r in status.json()["data"])


async def test_canary_endpoints_platform_admin_only(env):
    acme = await _create_tenant(env.client, "acme")
    tenant_admin = (
        await create_key(env.client, "acme-admin", is_admin=True, tenant_id=acme["id"])
    )["key"]
    plain_key = (await create_key(env.client, "plain"))["key"]

    # Deployment health is platform configuration (like /admin/routing):
    # tenant admins get 403, non-admin keys get 401/403.
    assert (
        await env.client.get("/admin/canaries", headers=auth(tenant_admin))
    ).status_code == 403
    assert (
        await env.client.post("/admin/canaries/probe", headers=auth(tenant_admin))
    ).status_code == 403
    for resp in (
        await env.client.get("/admin/canaries", headers=auth(plain_key)),
        await env.client.post("/admin/canaries/probe", headers=auth(plain_key)),
    ):
        assert resp.status_code in (401, 403)


async def test_canary_allowlist(tmp_path):
    # deployments: fnmatch allowlist — probe (and list) only the
    # deployments an operator names, e.g. to skip expensive previews.
    async for env in _make_env(
        tmp_path, config=make_config(canary=CanaryConfig(deployments=["openai/p*"]))
    ):
        results = await env.app.state.canary.probe_once()
        assert [r["deployment"] for r in results] == ["openai/primary"]
        # The snapshot lists the whole *matching* fleet, still.
        status = await env.client.get("/admin/canaries", headers=auth(ADMIN_KEY))
        assert [r["deployment"] for r in status.json()["data"]] == ["openai/primary"]
        # Filtered-out deployments were never touched.
        probed_models = {r["body"].get("model") for r in env.mock.requests}
        assert probed_models == {"model-a"}


async def test_canary_config_error_is_probe_error(tmp_path):
    # A deployment naming an unconfigured provider is a *probe* error
    # (surfaced per-deployment), never a crash of the wave — the same
    # containment the scheduled loop needs to survive bad config.
    config = make_config(
        models=[
            ModelConfig(
                name="ghost-model",
                deployments=[
                    DeploymentConfig(name="ghost/dep", provider="ghost", model="model-x")
                ],
            )
        ]
    )
    async for env in _make_env(tmp_path, config=config):
        results = await env.app.state.canary.probe_once()
        assert len(results) == 1
        assert results[0]["outcome"] == "error"
        assert "ghost" in results[0]["error"]
        # The probe-now endpoint reports it the same way.
        probed = (
            await env.client.post("/admin/canaries/probe", headers=auth(ADMIN_KEY))
        ).json()
        assert probed["healthy"] == 0
        assert probed["data"][0]["deployment"] == "ghost/dep"
