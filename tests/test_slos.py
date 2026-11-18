"""Service level objectives — tier 42: named promises about the
client experience, measured over the request logs and verdicted
honestly.

The contract pinned here, in one paragraph: ``slos:`` in config.yaml
declares targets — a metric (error_rate / latency percentiles /
ttft_p95), a ceiling, a rolling window and a sufficiency minimum —
scoped to a model or one deployment. ``GET /admin/slos``
(platform-admin-only; SLO definitions are platform health config)
measures each over the CLIENT EXPERIENCE rows (non-shadow,
non-trial; cache replays INCLUDED — a cached answer is a fast answer
the client really received) and verdicts: ``met`` (the ceiling
holds), ``breached`` (it does not, with the human reason saying by
how much) or ``insufficient_evidence`` (fewer than ``min_requests``
requests in the window — a quiet window is not a verdict — or no
measurement at all, e.g. a TTFT target where nothing streamed). The
numbers always ship beside the verdict, hot-reloaded config applies
on the next read, and the surface is the measurement — never a pager
(breach delivery is the next tier). Deployment scope counts only
ATTRIBUTED rows (the tier-40 story: a request that failed before any
dispatch completed has no deployment to blame, so it can neither
inflate nor flatter a deployment's SLO); MODEL scope counts the
unattributed failures too — the client experienced them.
"""

from __future__ import annotations

from gateway.config import SLOTargetConfig, SLOsConfig
from gateway.core.slos import evaluate
from gateway.db import get_sessionmaker
from gateway.models import RequestLog

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_pipeline import read_sse


def _target(**over) -> SLOTargetConfig:
    base = {
        "name": "t",
        "model": "test-model",
        "metric": "latency_p95_ms",
        "target": 800.0,
    }
    base.update(over)
    return SLOTargetConfig.model_validate(base)


def _measured(**over) -> dict:
    base = {
        "requests": 100,
        "errors": 0,
        "error_rate": 0.0,
        "latency_p50_ms": 100,
        "latency_p95_ms": 200,
        "latency_p99_ms": 300,
        "ttft_p95_ms": None,
    }
    base.update(over)
    return base


# ----------------------------------------------------------------------
# Units: evaluate — the pure verdict
# ----------------------------------------------------------------------


def test_evaluate_met_and_breached():
    """The ceiling is the whole story: at or under is met (no reason),
    over is breached with the reason saying the metric, the number
    and the bar."""
    met = evaluate(_measured(), _target())
    assert met["verdict"] == "met"
    assert met["check"] == {"value": 200, "pass": True}
    assert met["reason"] is None

    breached = evaluate(_measured(), _target(target=199.0))
    assert breached["verdict"] == "breached"
    assert breached["check"] == {"value": 200, "pass": False}
    assert "latency_p95_ms is 200" in breached["reason"]
    assert "199.0 bar" in breached["reason"]

    # The boundary is inclusive: exactly at the bar is met.
    edge = evaluate(_measured(), _target(target=200.0))
    assert edge["verdict"] == "met"


def test_evaluate_thin_volume_is_not_a_verdict():
    """Fewer than min_requests requests — even all failing — is
    insufficient evidence: a quiet window proves nothing about the
    service, and the sufficiency gate runs before the metric."""
    thin = evaluate(
        _measured(requests=3, errors=3, error_rate=1.0),
        _target(metric="error_rate", target=0.01, min_requests=20),
    )
    assert thin["verdict"] == "insufficient_evidence"
    assert thin["check"] == {"value": None, "pass": None}
    assert "only 3 request(s)" in thin["reason"]
    assert "minimum is 20" in thin["reason"]

    # The boundary: exactly min_requests requests is judged.
    enough = evaluate(
        _measured(requests=20, errors=0, error_rate=0.0),
        _target(metric="error_rate", target=0.01, min_requests=20),
    )
    assert enough["verdict"] == "met"


def test_evaluate_unmeasurable_is_insufficient_never_fake():
    """A metric with no measurement in the window (a TTFT target where
    nothing streamed) is insufficient evidence with its own reason —
    volume alone cannot rescue a metric that was never measured, and
    the value stays None rather than a fabricated number."""
    none = evaluate(
        _measured(ttft_p95_ms=None),
        _target(metric="ttft_p95_ms", target=300.0),
    )
    assert none["verdict"] == "insufficient_evidence"
    assert none["check"] == {"value": None, "pass": None}
    assert "no measurement for ttft_p95_ms" in none["reason"]

    measured = evaluate(
        _measured(ttft_p95_ms=250),
        _target(metric="ttft_p95_ms", target=300.0),
    )
    assert measured["verdict"] == "met"


# ----------------------------------------------------------------------
# e2e: the surface over real traffic
# ----------------------------------------------------------------------


async def _slos(client, **params):
    return await client.get("/admin/slos", params=params, headers=auth(ADMIN_KEY))


def _row(data: list[dict], name: str) -> dict:
    return next(r for r in data if r["name"] == name)


async def test_slos_over_real_traffic(tmp_path):
    """Real rows, real verdicts: streamed chats against the default
    mock (with a real upstream delay, so latency is never a flat 0)
    produce latency and TTFT measurements — the p95/error targets met
    under generous bars with min_requests=1, breached under an
    impossible 0.5ms bar, a min_requests=100 target honestly
    insufficient on test volumes, and the config echoed with every
    definition in declaration order while the rows sort by name."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(name="p95-generous", target=60000.0, min_requests=1),
                _target(name="p95-impossible", target=0.5, min_requests=1),
                _target(
                    name="error-clean", metric="error_rate", target=0.01, min_requests=1
                ),
                _target(
                    name="ttft", metric="ttft_p95_ms", target=60000.0, min_requests=1
                ),
                _target(
                    name="thin", metric="error_rate", target=0.01, min_requests=100
                ),
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        client = env.client
        # A real upstream delay so the latencies are real numbers (a
        # zero-delay mock can genuinely measure 0ms — not a bar an
        # "impossible" target can breach).
        env.mock.model_delays["model-a"] = 0.05
        # Two streamed chats: rows with latency AND ttft.
        for content in ("slo one", "slo two"):
            resp = await client.post(
                "/v1/chat/completions",
                json=chat_body(content=content, stream=True),
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 200
            await read_sse(resp)

        resp = await _slos(client)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [r["name"] for r in body["data"]] == [
            "error-clean",
            "p95-generous",
            "p95-impossible",
            "thin",
            "ttft",
        ]

        generous = _row(body["data"], "p95-generous")
        assert generous["model"] == "test-model"
        assert generous["deployment"] is None
        assert generous["window_minutes"] == 60
        assert generous["since"] is not None
        assert generous["measured"]["requests"] == 2
        assert generous["measured"]["latency_p95_ms"] is not None
        assert generous["verdict"] == "met"
        assert generous["check"]["pass"] is True

        impossible = _row(body["data"], "p95-impossible")
        assert impossible["verdict"] == "breached"
        assert impossible["check"]["value"] > 0.5
        assert impossible["check"]["pass"] is False
        assert impossible["reason"]

        clean = _row(body["data"], "error-clean")
        assert clean["value"] == 0.0
        assert clean["verdict"] == "met"

        ttft = _row(body["data"], "ttft")
        assert ttft["measured"]["ttft_p95_ms"] is not None
        assert ttft["verdict"] == "met"

        thin = _row(body["data"], "thin")
        assert thin["verdict"] == "insufficient_evidence"
        assert thin["check"] == {"value": None, "pass": None}  # never judged
        assert "minimum is 100" in thin["reason"]

        # The config echo: every definition, verbatim.
        assert [t["name"] for t in body["config"]["targets"]] == [
            "p95-generous",
            "p95-impossible",
            "error-clean",
            "ttft",
            "thin",
        ]
        break


async def test_slos_deployment_scope_counts_only_attributed_rows(tmp_path):
    """The tier-40 attribution story, SLO-shaped: seeded attributed
    rows (20 successes with exact latencies on one deployment, an
    unattributed error that never reached any deployment, a shadow
    mirror and a trial replay) — a deployment-scoped target sees ONLY
    its attributed rows (p95 exactly the seeded percentile, clean
    error rate), while a model-scoped target sees the unattributed
    failure too (the client experienced it) and never the mirror or
    the trial row."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="dep-p95",
                    deployment="openai/primary",
                    target=279.0,
                    min_requests=20,
                ),
                _target(
                    name="dep-errors",
                    deployment="openai/primary",
                    metric="error_rate",
                    target=0.0,
                    min_requests=20,
                ),
                _target(
                    name="model-errors",
                    metric="error_rate",
                    target=0.04,
                    min_requests=20,
                ),
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        async with get_sessionmaker()() as session:
            for i in range(20):  # attributed successes, latencies 100..290
                session.add(
                    RequestLog(
                        request_id=f"slo-dep-{i}",
                        model="test-model",
                        deployment="openai/primary",
                        status="success",
                        latency_ms=100 + i * 10,
                        shadow=False,
                        cache_hit=False,
                    )
                )
            # A failure that never reached any deployment: unattributed
            # (the router exhausted its ladder), but the model is set.
            session.add(
                RequestLog(
                    request_id="slo-unattributed",
                    model="test-model",
                    deployment=None,
                    status="error",
                    error_message="ladder exhausted",
                    shadow=False,
                )
            )
            # Evidence and experiments are not client experience.
            session.add(
                RequestLog(
                    request_id="slo-shadow",
                    model="test-model",
                    deployment="openai/twin",
                    status="error",
                    shadow=True,
                )
            )
            session.add(
                RequestLog(
                    request_id="slo-trial",
                    model="test-model",
                    deployment="openai/trial",
                    status="error",
                    trial_id="trial-1",
                )
            )
            await session.commit()

        resp = await _slos(env.client)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]

        dep = _row(data, "dep-p95")
        # p95 of 20 rows = sorted[round(0.95 * 19)] = 280 — the exact
        # nearest-rank the stats panel uses. Only the attributed rows
        # match the deployment scope (the shadow/trial exclusions are
        # proven by the model-scope row below).
        assert dep["measured"]["requests"] == 20
        assert dep["value"] == 280
        assert dep["verdict"] == "breached"  # 280 against a 279 bar
        assert dep["reason"]

        dep_err = _row(data, "dep-errors")
        assert dep_err["measured"]["errors"] == 0
        assert dep_err["value"] == 0.0
        assert dep_err["verdict"] == "met"

        model_err = _row(data, "model-errors")
        assert model_err["measured"]["requests"] == 21  # + the unattributed
        assert model_err["measured"]["errors"] == 1
        assert model_err["value"] == 1 / 21  # the same division, exactly
        assert model_err["verdict"] == "breached"  # 1/21 (~0.048) > the 0.04 bar
        break


async def test_slos_cache_hits_are_client_experience(tmp_path):
    """A cached answer is a fast answer the client really received:
    an identical repeat chat answers from the response cache, and the
    SLO window counts BOTH rows — the cache replays are not excluded
    the way deployment-health views exclude them."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="volume",
                    metric="error_rate",
                    target=0.01,
                    min_requests=2,
                ),
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        body = chat_body(content="cache me twice")
        for _ in range(2):
            resp = await env.client.post(
                "/v1/chat/completions", json=body, headers=auth(ADMIN_KEY)
            )
            assert resp.status_code == 200
        # The second really was a cache hit.
        logs = (
            await env.client.get(
                "/admin/logs", params={"limit": 10}, headers=auth(ADMIN_KEY)
            )
        ).json()["data"]
        assert sum(1 for r in logs if r["cache_hit"]) == 1

        resp = await _slos(env.client)
        row = _row(resp.json()["data"], "volume")
        assert row["measured"]["requests"] == 2  # the cache hit included
        assert row["measured"]["errors"] == 0
        assert row["verdict"] == "met"
        break


async def test_slos_permissions_and_empty_config(tmp_path):
    """Platform-admin-only (tenant admins and plain keys are 403 — SLO
    definitions are platform health configuration), and an empty
    config is empty data with the config echoed — no targets means no
    promises, not an error."""
    async for env in _make_env(tmp_path):
        client = env.client
        tenant = (
            await client.post(
                "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        tadmin = (
            await create_key(client, "tadmin", is_admin=True, tenant_id=tenant["id"])
        )["key"]
        plain = (await create_key(client, "plain"))["key"]
        for key in (tadmin, plain):
            resp = await client.get("/admin/slos", headers=auth(key))
            assert resp.status_code == 403, resp.text

        resp = await _slos(client)
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        assert resp.json()["config"]["targets"] == []
        break
