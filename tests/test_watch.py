"""Post-promotion watch — tier 40: the migration story continued
AFTER the promotion flips the candidate live.

The contract pinned here, in one paragraph: once a candidate is
promoted the mirrors stop, so the pair-shaped evidence ends exactly
when watching matters. ``GET /admin/migration/watch`` re-verdicts what
the logs still say — one row per SERVING (non-shadow) deployment, over
the attributed rows it carried (the client-facing rows logged per
request on the deployment that completed the dispatch; mirrors,
candidate-trial replays and cache replays excluded — evidence,
experiments and cache hits are not serving traffic). The attribution
is the honest part, pinned twice: a request that fails BEFORE any
dispatch completes (the router exhausts its ladder — a sole
deployment failing every attempt) logs its row with NO deployment,
so a failing candidate shows up as traffic that ran away (its
requests thin toward zero while its expected share stays put) —
never as attributed errors; the error rows that DO land on a
deployment are the ones after the dispatch completed — a stream that
dies mid-flight (``ctx.deployment`` is set once the stream starts,
before the iterator breaks). Each row watches from when the
deployment started serving the model (``since`` = its first serving
row — the promotion moment measured on the traffic itself, no side
state), compares against its same-priority-tier peers over exactly
that window (``baseline``), and verdicts with the same vocabulary,
the same ``migration:`` thresholds and the same honesty as the
pre-promotion verdicts: its own error rate on attributed rows
(``max_shadow_error_rate``) and its mean latency as a percent of the
peers' mean (``max_latency_delta_pct`` — unmeasurable means a null
check + caveat, never a fake number). ``no_go`` outranks ``hold``;
thin volume is ``insufficient_evidence`` regardless of the numbers;
a verdict is a RECOMMENDATION to keep serving or roll back — the
endpoint never pushes the rollback button itself.

Like the promotion/rollback tests, every environment here points
``config_path`` at a tmp_path file it owns: the repo's ./config.yaml
is never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gateway.config import GatewayConfig, MigrationConfig
from gateway.core.migration import evaluate_serving

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key
from .test_promotion import _post_promotion
from .test_trials import _chat, _drain

# The promotion test's E2E_YAML shape, with watch-shaped knobs: the
# incumbent carries an explicit weight (so a w=50 promotion splits the
# tier 50/50 for the latency comparison), a SOLO model exists (its only
# deployment is a shadow twin — promoted with the no-incumbent warning,
# it becomes a lone serving deployment whose failures can never be
# rescued by a fallback, which is how error rows land), and the
# migration thresholds are small enough to verdict on test volumes.
WATCH_YAML = """\
providers:
  openai:
    api_base: http://mockupstream.test/v1
    api_key: provider-key

models:
  - name: test-model
    deployments:
      # the incumbent, serving at an explicit weight
      - name: openai/primary
        provider: openai
        model: model-a
        weight: 50
      # the migration candidate: mirrored until promoted
      - name: openai/twin
        provider: openai
        model: model-shadow
        shadow: true
  - name: loner-model
    deployments:
      # a solo candidate: once promoted, nothing can rescue it
      - name: openai/loner
        provider: openai
        model: model-c
        shadow: true

pricing:
  model-a:
    input: 2.0
    output: 4.0
  model-shadow:
    input: 2.0
    output: 4.0
  model-c:
    input: 2.0
    output: 4.0

migration:
  min_pairs: 5
  max_latency_delta_pct: 25.0
"""

# The unit-test thresholds (matching the YAML above).
THRESHOLDS = MigrationConfig(min_pairs=5, max_latency_delta_pct=25.0)


def _write_config(tmp_path, text: str = WATCH_YAML):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


async def _watch(client, **params) -> object:
    import httpx

    return await client.get(
        "/admin/migration/watch", params=params, headers=auth(ADMIN_KEY)
    )


def _row(data: list[dict], deployment: str) -> dict:
    return next(r for r in data if r["deployment"] == deployment)


def _check(recommendation: dict, name: str) -> dict:
    return next(c for c in recommendation["checks"] if c["name"] == name)


# ----------------------------------------------------------------------
# Units: evaluate_serving — the pure verdict over serving stats
# ----------------------------------------------------------------------


def _serving_stats(**over) -> dict:
    base = {
        "requests": 10,
        "errors": 0,
        "latency_mean_ms": 100.0,
        "baseline_requests": 10,
        "baseline_errors": 0,
        "baseline_latency_mean_ms": 100.0,
    }
    base.update(over)
    return base


def test_evaluate_serving_clean_go():
    """Volume, a clean error rate and an on-par latency verdict go —
    every check measured, every check passed, no reasons."""
    result = evaluate_serving(_serving_stats(), THRESHOLDS)
    assert result["verdict"] == "go"
    assert {c["name"] for c in result["checks"]} == {
        "requests",
        "error_rate",
        "latency_delta_pct",
    }
    assert all(c["pass"] is True for c in result["checks"])
    assert _check(result, "requests")["value"] == 10
    assert _check(result, "error_rate")["value"] == 0.0
    assert _check(result, "latency_delta_pct")["value"] == 0.0
    assert result["reasons"] == []


def test_evaluate_serving_no_go_outranks_hold():
    """A failing error rate on a full volume is a no_go even when the
    latency is over its bar too — the worst honest call wins, and both
    reasons ship."""
    result = evaluate_serving(
        _serving_stats(errors=1, latency_mean_ms=200.0), THRESHOLDS
    )
    assert result["verdict"] == "no_go"
    assert _check(result, "error_rate")["pass"] is False
    assert _check(result, "error_rate")["value"] == 0.1
    assert _check(result, "latency_delta_pct")["pass"] is False
    assert _check(result, "latency_delta_pct")["value"] == 100.0
    assert len(result["reasons"]) == 2
    assert any("error rate" in r for r in result["reasons"])
    assert any("slower answers" in r for r in result["reasons"])


def test_evaluate_serving_latency_hold():
    """Answers fine, no errors, but slower than its tier peers: hold —
    the business-call verdict on serving traffic."""
    result = evaluate_serving(_serving_stats(latency_mean_ms=200.0), THRESHOLDS)
    assert result["verdict"] == "hold"
    assert _check(result, "error_rate")["pass"] is True
    assert _check(result, "latency_delta_pct")["pass"] is False
    assert len(result["reasons"]) == 1
    assert "slower answers" in result["reasons"][0]


def test_evaluate_serving_thin_insufficient_even_with_errors():
    """Volume gates the verdict FIRST: a handful of requests — even all
    failing — is insufficient evidence, never a no_go; a bad
    five-minute window is not a verdict."""
    result = evaluate_serving(
        _serving_stats(requests=2, errors=2, latency_mean_ms=None,
                        baseline_requests=0, baseline_latency_mean_ms=None),
        THRESHOLDS,
    )
    assert result["verdict"] == "insufficient_evidence"
    # The sufficiency gate is the only check: nothing else was judged.
    assert [c["name"] for c in result["checks"]] == ["requests"]
    assert _check(result, "requests")["pass"] is False
    assert "watch longer" in result["reasons"][0]


def test_evaluate_serving_no_baseline_go_with_caveat():
    """No peers carried traffic (a sole serving deployment, or a
    rescued candidate thinned to zero): the latency check is honestly
    null and a caveat says so — the verdict on the measurable checks
    is still go. The zero-peer-mean variant gets its own caveat
    wording, never a fake 0%."""
    result = evaluate_serving(
        _serving_stats(baseline_requests=0, baseline_errors=0,
                        baseline_latency_mean_ms=None),
        THRESHOLDS,
    )
    assert result["verdict"] == "go"
    latency = _check(result, "latency_delta_pct")
    assert latency["value"] is None and latency["pass"] is None
    assert result["reasons"] == [
        "latency unmeasured — no other serving deployment in its "
        "priority tier to compare latency against"
    ]

    zero_mean = evaluate_serving(
        _serving_stats(baseline_requests=10, baseline_latency_mean_ms=0.0),
        THRESHOLDS,
    )
    assert zero_mean["verdict"] == "go"
    assert _check(zero_mean, "latency_delta_pct")["value"] is None
    assert "latency unmeasured — tier peers carried no measurable " \
           "latency in the window" in zero_mean["reasons"]


# ----------------------------------------------------------------------
# e2e: the watch surface over real serving traffic
# ----------------------------------------------------------------------


async def test_watch_happy_path_excludes_evidence_and_verdicts_go(tmp_path):
    """The promoted candidate, watched: two pre-promotion mirror chats
    make the twin's evidence rows; the promotion flips it live at
    w=100; the incumbent is then made to fail so every one of 12
    distinct chats ends on the twin (rescued or direct — deterministic
    either way), and a 13th duplicate chat answers from the cache. The
    watch counts EXACTLY the 12 serving rows — the two mirror rows and
    the cache replay never reach it — and verdicts go: clean error
    rate, latency honestly unmeasured (the incumbent carried nothing
    in the window) with the caveat saying so. The incumbent's own row
    is the pre-promotion volume it carried, honestly insufficient."""
    import httpx

    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        client = env.client

        # Pre-promotion: two mirrored chats — evidence rows for the twin.
        for content in ("pre one", "pre two"):
            await _chat(client, content=content)
        await _drain(env)
        assert (
            await _post_promotion(
                client, "promote", model="test-model",
                deployment="openai/twin", weight=100,
            )
        ).status_code == 200

        # The incumbent stops answering: every chat now ends on the twin.
        env.mock.fail_counts["model-a"] = 999
        for i in range(12):
            rid = await _chat(client, content=f"post chat {i}")
        rid = await _chat(client, content="post chat 0")  # cache replay
        await _drain(env)

        # The exclusions are real: the twin carries 2 shadow rows (the
        # pre-promotion mirrors), 12 serving rows, and the duplicate
        # answered from the cache (its row flagged cache_hit, whatever
        # deployment it carries).
        logs = (
            await client.get(
                "/admin/logs", params={"limit": 500}, headers=auth(ADMIN_KEY)
            )
        ).json()["data"]
        twin_rows = [r for r in logs if r["deployment"] == "openai/twin"]
        assert len([r for r in twin_rows if r["shadow"]]) == 2
        assert len(
            [r for r in twin_rows if not r["shadow"] and not r["cache_hit"]]
        ) == 12
        assert any(r["cache_hit"] for r in logs)

        # The watch: serving rows only.
        resp = await _watch(client)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [r["deployment"] for r in body["data"]] == [
            "openai/primary", "openai/twin",
        ]
        assert body["thresholds"] == config.migration.model_dump()

        twin = _row(body["data"], "openai/twin")
        assert twin["model"] == "test-model"
        assert twin["requests"] == 12  # not 14 (mirrors), not 13 (cache)
        assert twin["errors"] == 0
        assert twin["error_rate"] == 0.0
        assert twin["actual_share"] == 1.0  # 12 of the tier's 12 rows
        assert twin["expected_share"] == round(100 / 150, 4)
        assert twin["since"] is not None
        assert twin["cost_mean_usd"] is not None and twin["cost_mean_usd"] > 0
        assert twin["baseline"] == {
            "deployments": ["openai/primary"],
            "requests": 0,
            "errors": 0,
            "error_rate": None,
            "latency_mean_ms": None,
        }
        rec = twin["recommendation"]
        assert rec["verdict"] == "go"
        assert _check(rec, "requests")["pass"] is True
        assert _check(rec, "error_rate")["pass"] is True
        latency = _check(rec, "latency_delta_pct")
        assert latency["value"] is None and latency["pass"] is None
        assert rec["reasons"] == [
            "latency unmeasured — no other serving deployment in its "
            "priority tier to compare latency against"
        ]

        primary = _row(body["data"], "openai/primary")
        assert primary["requests"] == 2  # the pre-promotion chats only
        assert primary["errors"] == 0
        assert primary["recommendation"]["verdict"] == "insufficient_evidence"

        # ?since= overrides every window: a future bound sees nothing,
        # echoes the bound, and verdicts the honest insufficient.
        future = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat()
        resp = await _watch(client, since=future)
        assert resp.status_code == 200
        empty = _row(resp.json()["data"], "openai/twin")
        assert empty["requests"] == 0
        assert empty["since"] == future
        assert empty["actual_share"] == 0.0
        assert empty["recommendation"]["verdict"] == "insufficient_evidence"
        break


async def test_watch_latency_hold_never_pinning_the_split(tmp_path):
    """A slower candidate serving beside its incumbent: w=50 against
    the incumbent's 50 splits the tier evenly, the candidate's
    upstream is delayed (+250ms vs ~50ms), and the watch holds it —
    the latency delta is pinned GENEROUSLY (weighted routing is
    probabilistic; only the split-independent facts are exact: both
    sides carried volume, the expected share is the config's 0.5,
    the delta is far over the bar, and the incumbent verdicts go."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        client = env.client
        assert (
            await _post_promotion(
                client, "promote", model="test-model",
                deployment="openai/twin", weight=50,
            )
        ).status_code == 200
        env.mock.delay_seconds = 0.05
        env.mock.model_delays["model-shadow"] = 0.2  # ADDS to the base

        for i in range(30):
            await _chat(client, content=f"split chat {i}")
        await _drain(env)

        resp = await _watch(client)
        assert resp.status_code == 200, resp.text
        twin = _row(resp.json()["data"], "openai/twin")
        primary = _row(resp.json()["data"], "openai/primary")

        # Split-independent pins.
        assert twin["requests"] >= 5
        assert twin["baseline"]["requests"] >= 5
        assert twin["expected_share"] == 0.5
        assert 0.25 <= twin["actual_share"] <= 0.75
        assert twin["errors"] == 0

        rec = twin["recommendation"]
        assert rec["verdict"] == "hold"
        latency = _check(rec, "latency_delta_pct")
        assert latency["pass"] is False
        assert latency["value"] is not None and latency["value"] > 100
        assert any("slower answers" in r for r in rec["reasons"])
        assert _check(rec, "error_rate")["pass"] is True

        # The incumbent, compared against the same peer over the same
        # window, is FASTER — a clean go.
        assert primary["recommendation"]["verdict"] == "go"
        primary_latency = _check(primary["recommendation"], "latency_delta_pct")
        assert primary_latency["pass"] is True
        assert primary_latency["value"] < 0
        break


async def test_watch_total_failure_is_unattributed_traffic_ran_away(tmp_path):
    """The honesty pin the tier almost missed: a request that fails
    BEFORE any dispatch completes — the sole deployment failing every
    attempt, retries and all — logs its row UNATTRIBUTED (no
    deployment: the router never finished a dispatch, so there is
    nothing honest to name). The watch therefore shows the promoted
    loner as traffic that ran away — requests 0 against an expected
    share of 1.0, insufficient_evidence — never a fabricated no_go
    from rows it cannot attribute. The error rows themselves are
    pinned here, deployment and all (None), straight off the logs."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        client = env.client
        promotion = await _post_promotion(
            client, "promote", model="loner-model",
            deployment="openai/loner", weight=100,
        )
        assert promotion.status_code == 200, promotion.text
        assert promotion.json()["warnings"]  # no incumbent + thin evidence

        env.mock.fail_counts["model-c"] = 999
        for i in range(6):
            resp = await client.post(
                "/v1/chat/completions",
                json=chat_body(model="loner-model", content=f"boom {i}"),
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code >= 400  # nothing rescues a sole deployment
        await _drain(env)

        # The rows exist, model and all — but no deployment: the
        # dispatch never completed, so the router had nothing to name.
        logs = (
            await client.get(
                "/admin/logs", params={"limit": 500}, headers=auth(ADMIN_KEY)
            )
        ).json()["data"]
        loner_rows = [r for r in logs if r["model"] == "loner-model"]
        assert len(loner_rows) == 6
        assert all(r["status"] == "error" for r in loner_rows)
        assert {r["deployment"] for r in loner_rows} == {None}

        # The watch: the unattributed rows never inflate any
        # deployment — the loner shows the traffic-ran-away shape.
        resp = await _watch(client, model="loner-model")
        assert resp.status_code == 200, resp.text
        row = _row(resp.json()["data"], "openai/loner")
        assert row["requests"] == 0
        assert row["errors"] == 0
        assert row["error_rate"] is None
        assert row["since"] is None  # it never completed a dispatch
        assert row["expected_share"] == 1.0 and row["actual_share"] == 0.0
        rec = row["recommendation"]
        assert rec["verdict"] == "insufficient_evidence"
        assert [c["name"] for c in rec["checks"]] == ["requests"]
        assert "watch longer" in rec["reasons"][0]
        break


async def test_watch_no_go_on_attributed_errors(tmp_path):
    """The no_go that real serving traffic produces: errors that land
    ON the deployment — the mid-stream story (ctx.deployment is set
    once the stream starts, BEFORE the iterator breaks, so a stream
    that dies mid-flight logs its error row on the deployment that was
    serving it; the atomically-buffered MockUpstream cannot simulate
    that — its failures happen at connect and log unattributed, pinned
    above — so the rows are seeded as the stream wrapper would have
    written them). Six good requests plus two dying streams = a 25%
    error rate over the 2% bar: no_go, the same SLO the mirror
    verdicts judged by, measured on serving traffic."""
    from gateway.db import get_sessionmaker
    from gateway.models import RequestLog

    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        client = env.client
        async with get_sessionmaker()() as session:
            for i in range(6):  # six healthy requests
                session.add(
                    RequestLog(
                        request_id=f"seed-ok-{i}", model="test-model",
                        deployment="openai/primary", status="success",
                        latency_ms=120, cost_usd=0.001,
                    )
                )
            for i in range(2):  # two streams that died mid-flight
                session.add(
                    RequestLog(
                        request_id=f"seed-err-{i}", model="test-model",
                        deployment="openai/primary", status="error",
                        error_message="stream broke mid-flight",
                        latency_ms=80, shadow=False, cache_hit=False,
                    )
                )
            await session.commit()

        resp = await _watch(client)
        assert resp.status_code == 200, resp.text
        primary = _row(resp.json()["data"], "openai/primary")
        assert primary["requests"] == 8
        assert primary["errors"] == 2
        assert primary["error_rate"] == 0.25
        rec = primary["recommendation"]
        assert rec["verdict"] == "no_go"
        assert _check(rec, "requests")["pass"] is True
        assert _check(rec, "error_rate")["pass"] is False
        assert any("error rate" in r for r in rec["reasons"])
        # No serving peers: latency honestly unmeasured on top.
        assert any("latency unmeasured" in r for r in rec["reasons"])
        break


async def test_watch_rescued_candidate_thins_out(tmp_path):
    """The traffic-ran-away story: a failing candidate rescued by its
    incumbent leaves NO row on itself (the fallback's rescue ends the
    request on the incumbent), so the watch sees the candidate's
    requests thin to zero while its expected share stays put —
    requests == 0 verdicts insufficient_evidence, and the share split
    (expected ~0.67, actual 0.0) is the loud part. The incumbent
    carried every request, verdicting go with the same caveat."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        client = env.client
        assert (
            await _post_promotion(
                client, "promote", model="test-model",
                deployment="openai/twin", weight=100,
            )
        ).status_code == 200
        env.mock.fail_counts["model-shadow"] = 999  # the candidate breaks

        for i in range(8):
            await _chat(client, content=f"rescue me {i}")
        await _drain(env)

        resp = await _watch(client)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        twin = _row(data, "openai/twin")
        primary = _row(data, "openai/primary")

        # The candidate thinned out: no row ever ended on it.
        assert twin["requests"] == 0
        assert twin["errors"] == 0
        assert twin["error_rate"] is None
        assert twin["actual_share"] == 0.0
        assert twin["expected_share"] == round(100 / 150, 4)
        assert twin["since"] is None  # it never served at all
        rec = twin["recommendation"]
        assert rec["verdict"] == "insufficient_evidence"
        assert [c["name"] for c in rec["checks"]] == ["requests"]
        assert "watch longer" in rec["reasons"][0]

        # The incumbent carried every request, all rescued.
        assert primary["requests"] == 8
        assert primary["errors"] == 0
        assert primary["actual_share"] == 1.0
        assert primary["recommendation"]["verdict"] == "go"
        break


async def test_watch_permissions_filters_and_zero_traffic(tmp_path):
    """Platform-admin-only like every migration surface; a malformed
    since is the standard 400; unknown model/deployment are empty
    data (not errors) with the thresholds still echoed; shadow twins
    never appear (they carry no serving traffic); and a serving
    deployment with no traffic yet is the honest zero row: requests
    0, actual share 0.0, insufficient_evidence."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        client = env.client

        tenant = (
            await client.post(
                "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        tadmin = (
            await create_key(
                client, "tadmin", is_admin=True, tenant_id=tenant["id"]
            )
        )["key"]
        plain = (await create_key(client, "plain"))["key"]
        for key in (tadmin, plain):
            resp = await client.get(
                "/admin/migration/watch", headers=auth(key)
            )
            assert resp.status_code == 403, resp.text

        malformed = await _watch(client, since="not-a-date")
        assert malformed.status_code == 400
        assert malformed.json()["error"]["type"] == "invalid_request_error"

        unknown = await _watch(client, model="no-such-model")
        assert unknown.status_code == 200
        assert unknown.json()["data"] == []
        assert unknown.json()["thresholds"]["min_pairs"] == 5

        unknown_dep = await _watch(client, deployment="openai/twin")
        assert unknown_dep.status_code == 200
        assert unknown_dep.json()["data"] == []  # the twin is a mirror, not serving

        # The one serving deployment, zero traffic: the honest zero row.
        resp = await _watch(client)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert [r["deployment"] for r in data] == ["openai/primary"]
        primary = data[0]
        assert primary["requests"] == 0
        assert primary["errors"] == 0
        assert primary["error_rate"] is None
        assert primary["since"] is None
        assert primary["expected_share"] == 1.0
        assert primary["actual_share"] == 0.0
        assert primary["baseline"] == {
            "deployments": [],
            "requests": 0,
            "errors": 0,
            "error_rate": None,
            "latency_mean_ms": None,
        }
        assert primary["recommendation"]["verdict"] == "insufficient_evidence"
        break
