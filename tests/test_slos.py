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

Tier 43 — the error-budget lens on the same rows: every metric
restated as a bad-event fraction against its allowance (the bar
itself for error_rate; the implied 1-p tail for the percentile
metrics), burn rate = observed / allowed, budget remaining, and
the multiwindow ``slos.burn`` rules — the SRE anti-flap AND where a
long window establishes the pace and a short one confirms it is
STILL burning (a burst that already ended never fires). Budget
states: on_track / at_risk (within allowance but a rule is
burning — fine on average, spending fast NOW) / exhausted (the
window overspent) / insufficient_evidence (thin window, or a
zero-tolerance bar's rules, where burn is undefined and stays None
rather than an invented infinity).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from gateway.config import SLOBurnRule, SLOTargetConfig, SLOsConfig
from gateway.core.slos import (
    allowed_bad_fraction,
    budget_state,
    burn_rule_verdict,
    evaluate,
)
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


# ----------------------------------------------------------------------
# Tier 43 — error budgets & burn rate: units (the pure layer)
# ----------------------------------------------------------------------


def _w(minutes: int, measured: int, bad: int) -> dict:
    """A measured window dict, as measure_bad returns."""
    return {
        "minutes": minutes,
        "measured": measured,
        "bad": bad,
        "bad_fraction": (bad / measured) if measured else None,
    }


def test_allowed_bad_fraction_is_the_bar_restated():
    """The allowance mapping: error_rate's allowance is the bar
    itself; a percentile bar implies the 1-p tail may exceed it —
    the same promise seen from the budget side."""
    assert allowed_bad_fraction(
        _target(metric="error_rate", target=0.01)
    ) == 0.01
    assert allowed_bad_fraction(_target(metric="latency_p50_ms", target=800)) == 0.50
    assert allowed_bad_fraction(_target(metric="latency_p95_ms", target=800)) == 0.05
    assert allowed_bad_fraction(_target(metric="latency_p99_ms", target=800)) == 0.01
    assert allowed_bad_fraction(_target(metric="ttft_p95_ms", target=300)) == 0.05


def test_budget_state_machine():
    """The pure budget verdict: on_track within allowance, exhausted
    when the window overspends, at_risk when a burn rule fires while
    the average still holds, insufficient on thin volume — and the
    zero-tolerance special case, where burn is undefined (None,
    never an invented infinity) but any bad event exhausts."""
    slo = _target(metric="error_rate", target=0.05, window_minutes=1440, min_requests=20)

    on_track = budget_state(_w(1440, 1000, 10), slo, any_burning=False)
    assert on_track["state"] == "on_track"
    assert on_track["burn_rate"] == (10 / 1000) / 0.05
    assert abs(on_track["remaining_pct"] - 80.0) < 1e-9  # a fifth of the allowance spent
    assert on_track["reason"] is None

    exhausted = budget_state(_w(1440, 1000, 100), slo, any_burning=False)
    assert exhausted["state"] == "exhausted"
    assert abs(exhausted["remaining_pct"] + 100.0) < 1e-9  # overspent by the whole allowance
    assert "overspent" in exhausted["reason"]

    at_risk = budget_state(_w(1440, 1000, 10), slo, any_burning=True)
    assert at_risk["state"] == "at_risk"  # fine on average, burning fast NOW
    assert "burn rule is firing" in at_risk["reason"]

    thin = budget_state(_w(1440, 3, 3), slo, any_burning=False)
    assert thin["state"] == "insufficient_evidence"
    assert "minimum is 20" in thin["reason"]

    zero = _target(metric="error_rate", target=0.0, min_requests=20)
    zt_spent = budget_state(_w(60, 30, 1), zero, any_burning=False)
    assert zt_spent["state"] == "exhausted"
    assert zt_spent["burn_rate"] is None and zt_spent["remaining_pct"] is None
    assert "zero-tolerance" in zt_spent["reason"]
    zt_clean = budget_state(_w(60, 30, 0), zero, any_burning=False)
    assert zt_clean["state"] == "on_track"
    assert "no bad events" in zt_clean["reason"]


def test_burn_rule_two_window_and():
    """The SRE anti-flap: burning needs BOTH windows over the bar —
    the long window says the pace is real, the short one says it is
    still burning now; a burst that already ended reads steady.
    Thin windows read insufficient with the scaled gate in the
    reason, and a zero-tolerance bar has no rate to compare."""
    slo = _target(metric="error_rate", target=0.05, window_minutes=1440, min_requests=20)
    rule = SLOBurnRule(name="fast", rate=14.4, long_minutes=60, short_minutes=5)
    # The gates scale with the window's share of the target's own
    # window: 20-of-a-day is 1 in an hour and 1 in five minutes.
    assert rule.long_minutes == 60 and rule.short_minutes == 5

    burning = burn_rule_verdict(_w(60, 42, 32), _w(5, 10, 8), rule, slo)
    assert burning["verdict"] == "burning"
    assert burning["long"]["burn_rate"] == (32 / 42) / 0.05
    assert burning["short"]["burn_rate"] == 0.8 / 0.05
    assert "confirmed" in burning["reason"]

    # The burst ended: pace window hot, confirmation window calm —
    # the AND refuses to page on what already stopped.
    antiflap = burn_rule_verdict(_w(60, 32, 24), _w(5, 3, 0), rule, slo)
    assert antiflap["verdict"] == "steady"
    assert antiflap["reason"] is None

    # Still hot now but not over the pace bar — steady, not burning.
    ended_late = burn_rule_verdict(_w(60, 32, 2), _w(5, 3, 3), rule, slo)
    assert ended_late["verdict"] == "steady"

    # A thin confirmation window is honest insufficiency, with the
    # scaled gate in the reason (20-of-an-hour is 2 in five minutes).
    busy_hour = _target(metric="error_rate", target=0.05, window_minutes=60, min_requests=20)
    thin_short = burn_rule_verdict(_w(60, 500, 100), _w(5, 1, 1), rule, busy_hour)
    assert thin_short["verdict"] == "insufficient_evidence"
    assert "confirmation window" in thin_short["reason"]
    assert thin_short["short"]["gate"] == 2

    zero = _target(metric="error_rate", target=0.0, min_requests=20)
    zt = burn_rule_verdict(_w(60, 100, 50), _w(5, 10, 9), rule, zero)
    assert zt["verdict"] == "insufficient_evidence"
    assert "zero-tolerance" in zt["reason"]


def test_burn_rule_bad_config_rejected():
    """The confirmation window must sit inside the pace window — a
    short window wider than the long one confirms nothing, and the
    config says so at parse time."""
    with pytest.raises(ValidationError, match="short_minutes"):
        SLOBurnRule(name="broken", rate=14.4, long_minutes=5, short_minutes=60)
    # The canonical shape parses fine.
    SLOBurnRule(name="ok", rate=14.4, long_minutes=60, short_minutes=5)


# ----------------------------------------------------------------------
# Tier 43 — error budgets & burn rate: e2e over seeded windows
# ----------------------------------------------------------------------


async def _seed(session, rows: list[tuple[str, str | None, int]]) -> None:
    """Seed (status, latency_ms, minutes_ago) rows for test-model at
    controlled ages — the window boundaries tier 43 measures."""
    now = datetime.now(timezone.utc)
    for i, (status, latency, minutes_ago) in enumerate(rows):
        session.add(
            RequestLog(
                request_id=f"t43-{i}",
                model="test-model",
                deployment="openai/primary",
                status=status,
                latency_ms=latency,
                created_at=now - timedelta(minutes=minutes_ago),
            )
        )
    await session.commit()


def _by_name(data: list[dict], name: str) -> dict:
    return next(r for r in data if r["name"] == name)


async def test_budget_at_risk_burns_fast_within_allowance(tmp_path):
    """The tier's thesis in one row: the bar verdict is MET (the
    24h window is within its 5% allowance — burn 0.61x) while the
    fast-burn rule is BURNING (76% errors in the last hour,
    confirmed 80% in the last five minutes) — so the budget reads
    at_risk: fine on average, spending fast right now. The slow rule
    stays steady: its pace window (the day) is under the 3x bar, and
    the two-window AND means a hot confirmation window alone pages
    no one."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="svc",
                    metric="error_rate",
                    target=0.05,
                    window_minutes=1440,
                    min_requests=20,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        rows: list[tuple[str, str | None, int]] = []
        # A quiet day carrying the average: 1000 good rows, 3-23h old.
        rows += [("success", 100, m) for m in range(200, 1400, 1)][:1000]
        # A bad hour, 10-33 minutes ago: 24 bad + 8 good.
        rows += [("error", 90, m) for m in range(10, 34)][:24]
        rows += [("success", 100, m) for m in range(10, 30)][:8]
        # And it is still burning now: 8 bad + 2 good in the last 2m.
        rows += [("error", 90, 2)] * 8 + [("success", 100, 2)] * 2
        async with get_sessionmaker()() as session:
            await _seed(session, rows)

        data = (await _slos(env.client)).json()["data"]
        row = _by_name(data, "svc")

        # The bar verdict: met — the day is within its allowance.
        assert row["verdict"] == "met"
        assert row["value"] == 32 / 1042

        b = row["budget"]
        assert b["allowed_fraction"] == 0.05
        assert b["measured"] == 1042 and b["bad"] == 32
        assert b["burn_rate"] == (32 / 1042) / 0.05
        assert b["state"] == "at_risk"
        assert b["remaining_pct"] > 0  # within allowance — that is the point
        assert "burn rule is firing" in b["reason"]

        fast = _by_name(row["burn"], "fast-burn")
        assert fast["verdict"] == "burning"
        assert fast["long"]["measured"] == 42 and fast["long"]["bad"] == 32
        assert fast["long"]["burn_rate"] == (32 / 42) / 0.05  # 15.2x >= 14.4
        assert fast["short"]["measured"] == 10 and fast["short"]["bad"] == 8
        assert fast["short"]["burn_rate"] == 0.8 / 0.05  # 16x, still burning
        assert "confirmed" in fast["reason"]

        slow = _by_name(row["burn"], "slow-burn")
        assert slow["verdict"] == "steady"  # the day itself is calm (0.61x < 3)
        break


async def test_budget_antiflap_burst_that_ended(tmp_path):
    """The confirmation window earns its keep: the same bad hour,
    but twenty-plus minutes old — the pace window is hot (15x) while
    the last five minutes are clean, so the rule reads steady and
    nobody gets paged for a burst that already stopped. The budget
    itself stays on_track (the day is within allowance and nothing is
    burning now)."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="svc",
                    metric="error_rate",
                    target=0.05,
                    window_minutes=1440,
                    min_requests=20,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        rows: list[tuple[str, str | None, int]] = []
        rows += [("success", 100, m) for m in range(200, 1400, 1)][:500]
        # The burst: 26 bad + 6 good, 20-45 minutes ago — inside the
        # hour (15.8x the allowed pace), well outside the five-minute
        # confirmation window.
        rows += [("error", 90, m) for m in range(20, 46)][:26]
        rows += [("success", 100, m) for m in range(20, 26)][:6]
        # Calm now: one clean request for the confirmation window to
        # judge (its gate is 1 — 20-of-a-day scaled to five minutes).
        rows += [("success", 100, 2)] * 1
        async with get_sessionmaker()() as session:
            await _seed(session, rows)

        data = (await _slos(env.client)).json()["data"]
        row = _by_name(data, "svc")
        fast = _by_name(row["burn"], "fast-burn")
        assert fast["long"]["measured"] == 33 and fast["long"]["bad"] == 26
        assert fast["long"]["burn_rate"] == (26 / 33) / 0.05  # 15.8x — hot
        assert fast["short"]["measured"] == 1 and fast["short"]["bad"] == 0
        assert fast["short"]["sufficient"] is True  # judged, honestly steady
        assert fast["verdict"] == "steady"  # the AND refuses the ended burst
        assert fast["reason"] is None

        assert row["budget"]["state"] == "on_track"
        assert row["budget"]["burn_rate"] == (26 / 533) / 0.05  # 0.98x — within allowance
        assert row["verdict"] == "met"  # the bar agrees: 26/533 <= 5%
        break


async def test_budget_slow_burn_grind_and_exhaustion(tmp_path):
    """The other edge: a uniform 20% error grind over the whole day —
    no burst, nothing special about the last hour — where the slow
    rule fires (4x over the day, confirmed in the last two hours)
    while the fast rule stays steady (no 14.4x hour), and the budget
    is honestly EXHAUSTED: the window spent 4x its allowance, so
    remaining reads negative and the bar verdict is breached too."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="svc",
                    metric="error_rate",
                    target=0.05,
                    window_minutes=1440,
                    min_requests=20,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        rows: list[tuple[str, str | None, int]] = []
        # The grind: 20% errors spread over 2.5-23h — no burst.
        rows += [("error", 90, m) for m in range(150, 1400, 4)][:80]
        rows += [("success", 100, m) for m in range(151, 1400, 1)][:320]
        # The last two hours grind too (the slow rule's confirmation):
        # 15 rows with 3 bad aged 10-60m, 15 rows with 3 bad aged 70-100m.
        rows += [("error", 90, m) for m in range(10, 60, 15)][:3]
        rows += [("success", 100, m) for m in range(11, 60, 3)][:12]
        rows += [("error", 90, m) for m in range(70, 100, 10)][:3]
        rows += [("success", 100, m) for m in range(71, 100, 2)][:12]
        # Calm right now, so the fast rule has nothing to say.
        rows += [("success", 100, 2)] * 2
        async with get_sessionmaker()() as session:
            await _seed(session, rows)

        data = (await _slos(env.client)).json()["data"]
        row = _by_name(data, "svc")

        slow = _by_name(row["burn"], "slow-burn")
        assert slow["verdict"] == "burning"
        assert slow["long"]["measured"] == 432 and slow["long"]["bad"] == 86
        assert slow["long"]["burn_rate"] >= 3.0  # the day, ~4x
        assert slow["short"]["measured"] == 32 and slow["short"]["bad"] == 6
        assert slow["short"]["burn_rate"] >= 3.0  # the last 2h, ~3.75x

        fast = _by_name(row["burn"], "fast-burn")
        assert fast["verdict"] == "steady"  # no 14.4x hour — no page

        b = row["budget"]
        assert b["state"] == "exhausted"
        assert b["remaining_pct"] < -100  # ~4x the allowance spent
        assert "overspent" in b["reason"]
        assert row["verdict"] == "breached"  # the bar agrees: 86/432 > 5%
        break


async def test_burn_thin_windows_are_insufficient(tmp_path):
    """The scaled gate keeps quiet windows honest: with a
    min_requests=200 floor on an hour, a hundred-request burst —
    half of them failures, a scary 50% rate — is insufficient
    evidence, not a fire alarm; the reason names the window and the
    gate. The budget reads the same: no volume, no budget to
    read."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="quiet",
                    metric="error_rate",
                    target=0.01,
                    window_minutes=60,
                    min_requests=200,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        rows: list[tuple[str, str | None, int]] = []
        rows += [("error", 90, 1)] * 50 + [("success", 100, 1)] * 50
        async with get_sessionmaker()() as session:
            await _seed(session, rows)

        data = (await _slos(env.client)).json()["data"]
        row = _by_name(data, "quiet")
        fast = _by_name(row["burn"], "fast-burn")
        assert fast["verdict"] == "insufficient_evidence"
        assert "60m pace window" in fast["reason"]
        assert fast["long"]["gate"] == 200  # min_requests, same window: unscaled
        assert fast["short"]["gate"] == 17  # 200 scaled by 5/60, floored at 1
        assert row["budget"]["state"] == "insufficient_evidence"
        assert "minimum is 200" in row["budget"]["reason"]
        break


async def test_budget_latency_bad_events(tmp_path):
    """A latency bar's budget: the bad events are the rows OVER the
    bar and the allowance is the implied 5% tail — 6 of 100 rows
    over 300ms spends 1.2x the allowance (exhausted, remaining
    -20%), computed over the LATENCY-BEARING rows only (the five
    error rows without a latency measurement change neither side),
    and the percentile verdict agrees: p95 IS one of the over-bar
    rows, so the bar reads breached too."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="slow",
                    metric="latency_p95_ms",
                    target=300.0,
                    window_minutes=60,
                    min_requests=20,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        rows: list[tuple[str, str | None, int]] = []
        # Aged 2m — safely inside even the 5m confirmation window
        # (a row seeded at exactly the window edge falls outside it
        # once the clock advances past the seed).
        rows += [("success", 100, 2)] * 94
        rows += [("success", 400, 2)] * 6
        rows += [("error", None, 2)] * 5  # no latency to measure
        async with get_sessionmaker()() as session:
            await _seed(session, rows)

        data = (await _slos(env.client)).json()["data"]
        row = _by_name(data, "slow")
        assert row["verdict"] == "breached"
        assert row["value"] == 400  # p95 lands on an over-bar row

        b = row["budget"]
        assert b["allowed_fraction"] == 0.05  # p95 => 5% may exceed the bar
        assert b["measured"] == 100  # latency-bearing rows only
        assert b["bad"] == 6
        assert b["burn_rate"] == 0.06 / 0.05
        assert b["state"] == "exhausted"
        assert abs(b["remaining_pct"] + 20.0) < 1e-9
        # The burn rules measure the same bad events — no 14.4x hour
        # of over-bar rows, so honestly steady.
        fast = _by_name(row["burn"], "fast-burn")
        assert fast["verdict"] == "steady"
        assert fast["long"]["bad"] == 6 and fast["long"]["measured"] == 100
        break


async def test_budget_ttft_over_streamed_rows(tmp_path):
    """A TTFT bar's budget counts streamed rows only — the rows with
    a first token to measure — and the 30 non-streamed rows change
    neither the denominator nor the verdict: 5 of 25 streamed
    requests over the 200ms bar burns 4x the 5% allowance."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="ttft",
                    metric="ttft_p95_ms",
                    target=200.0,
                    window_minutes=60,
                    min_requests=20,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        async with get_sessionmaker()() as session:
            now = datetime.now(timezone.utc)
            for i in range(20):
                session.add(
                    RequestLog(
                        request_id=f"t43-t-{i}", model="test-model", status="success",
                        latency_ms=150, ttft_ms=50, created_at=now - timedelta(minutes=2),
                    )
                )
            for i in range(5):
                session.add(
                    RequestLog(
                        request_id=f"t43-tb-{i}", model="test-model", status="success",
                        latency_ms=400, ttft_ms=300, created_at=now - timedelta(minutes=2),
                    )
                )
            for i in range(30):  # non-streamed: no first token to measure
                session.add(
                    RequestLog(
                        request_id=f"t43-tn-{i}", model="test-model", status="success",
                        latency_ms=120, ttft_ms=None, created_at=now - timedelta(minutes=2),
                    )
                )
            await session.commit()

        data = (await _slos(env.client)).json()["data"]
        row = _by_name(data, "ttft")
        b = row["budget"]
        assert b["measured"] == 25  # streamed rows only
        assert b["bad"] == 5  # ttft 300 > 200
        assert b["burn_rate"] == (5 / 25) / 0.05
        assert b["state"] == "exhausted"
        assert row["verdict"] == "breached"  # ttft p95 = 300 > 200
        break


async def test_budget_zero_tolerance_never_invents_infinity(tmp_path):
    """A zero-tolerance bar (error_rate 0) has no rate to compute, so
    the budget says what happened instead of inventing an infinity:
    clean reads on_track with no bad events, a single failure reads
    exhausted — and the burn rules are insufficient with the reason
    saying the rate is undefined, while the bar verdict agrees
    (breached) because any error is over a 0 bar."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(name="zt-clean", metric="error_rate", target=0.0, min_requests=20),
                _target(
                    name="zt-spent", metric="error_rate", target=0.0,
                    model="other-model", min_requests=20,
                ),
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        async with get_sessionmaker()() as session:
            now = datetime.now(timezone.utc)
            for i in range(30):
                session.add(
                    RequestLog(
                        request_id=f"t43-zc-{i}", model="test-model", status="success",
                        latency_ms=100, created_at=now - timedelta(minutes=5),
                    )
                )
            for i in range(29):
                session.add(
                    RequestLog(
                        request_id=f"t43-zs-{i}", model="other-model", status="success",
                        latency_ms=100, created_at=now - timedelta(minutes=5),
                    )
                )
            session.add(
                RequestLog(
                    request_id="t43-zs-bad", model="other-model", status="error",
                    created_at=now - timedelta(minutes=5),
                )
            )
            await session.commit()

        data = (await _slos(env.client)).json()["data"]
        clean = _by_name(data, "zt-clean")
        assert clean["budget"]["state"] == "on_track"
        assert clean["budget"]["burn_rate"] is None
        assert clean["budget"]["remaining_pct"] is None
        assert "no bad events" in clean["budget"]["reason"]
        assert clean["verdict"] == "met"  # 0 errors <= 0
        assert all(
            r["verdict"] == "insufficient_evidence" and "zero-tolerance" in r["reason"]
            for r in clean["burn"]
        )

        spent = _by_name(data, "zt-spent")
        assert spent["budget"]["state"] == "exhausted"
        assert spent["budget"]["burn_rate"] is None  # no rate, never infinity
        assert spent["budget"]["bad"] == 1
        assert "1 bad event" in spent["budget"]["reason"]
        assert spent["verdict"] == "breached"  # 1/30 > 0
        break


async def test_budget_rules_config_echo(tmp_path):
    """The default rules are the SRE workbook's canonical pairs and
    echo in the config verbatim, one verdict block per rule per
    target."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="svc",
                    metric="error_rate",
                    target=0.05,
                    window_minutes=1440,
                    min_requests=20,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        async with get_sessionmaker()() as session:
            await _seed(session, [("success", 100, 5)] * 40)
        body = (await _slos(env.client)).json()
        assert [(r["name"], r["rate"]) for r in body["config"]["burn"]] == [
            ("fast-burn", 14.4),
            ("slow-burn", 3.0),
        ]
        row = _by_name(body["data"], "svc")
        assert [r["name"] for r in row["burn"]] == ["fast-burn", "slow-burn"]
        assert row["budget"]["state"] == "on_track"  # 0 bad events, 40 rows
        break


async def test_budget_burn_rules_disabled(tmp_path):
    """``burn: []`` disables the pace verdicts (rows carry an empty
    list) while the budget math stands on its own — and can never
    read at_risk with no rules to fire."""
    disabled = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="svc",
                    metric="error_rate",
                    target=0.05,
                    window_minutes=1440,
                    min_requests=20,
                )
            ],
            burn=[],
        )
    )
    async for env in _make_env(tmp_path, config=disabled):
        async with get_sessionmaker()() as session:
            await _seed(session, [("success", 100, 5)] * 40)
        row = _by_name((await _slos(env.client)).json()["data"], "svc")
        assert row["burn"] == []
        assert row["budget"]["state"] == "on_track"
        assert row["budget"]["measured"] == 40
        assert (await _slos(env.client)).json()["config"]["burn"] == []
        break


# ----------------------------------------------------------------------
# Route scoping (tier 66): targets narrowed to one inbound surface —
# the MCP server rows (model = the server name, route "mcp") can be
# promised separately from chat rows that share the name.
# ----------------------------------------------------------------------


def _seed_routed(session, *, route: str, model: str, rows: list[dict]) -> None:
    """Rows on one inbound surface (route set), controlled status and
    latency — MCP-shaped when route="mcp": never streamed (ttft NULL),
    no deployment attribution."""
    now = datetime.now(timezone.utc)
    for i, spec in enumerate(rows):
        session.add(
            RequestLog(
                request_id=f"t66-{route}-{i}",
                model=model,
                route=route,
                deployment=spec.get("deployment"),
                status=spec["status"],
                latency_ms=spec.get("latency_ms"),
                created_at=now - timedelta(minutes=spec.get("minutes_ago", 0)),
                shadow=False,
                cache_hit=False,
            )
        )


async def test_route_scope_measures_only_that_surface(tmp_path):
    """A target with route="mcp" sees ONLY the tool-server rows; the
    same model name without a route sees everything (the client's whole
    experience of the name). Here: 10 healthy MCP rows beside 5 chat
    rows carrying 2 errors — the scoped target is met on its own rows,
    the unscoped one is breached on all 15."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="tool-server",
                    model="jira",
                    route="mcp",
                    metric="error_rate",
                    target=0.05,
                    min_requests=5,
                ),
                _target(
                    name="everything",
                    model="jira",
                    metric="error_rate",
                    target=0.05,
                    min_requests=5,
                ),
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        async with get_sessionmaker()() as session:
            _seed_routed(
                session,
                route="mcp",
                model="jira",
                rows=[{"status": "success", "latency_ms": 100 + i * 10} for i in range(10)],
            )
            _seed_routed(
                session,
                route="chat",
                model="jira",
                rows=[
                    {"status": "success", "latency_ms": 300},
                    {"status": "success", "latency_ms": 320},
                    {"status": "success", "latency_ms": 340},
                    {"status": "error"},
                    {"status": "error"},
                ],
            )
            await session.commit()

        data = (await _slos(env.client)).json()["data"]
        scoped = _by_name(data, "tool-server")
        assert scoped["route"] == "mcp"  # the scope echoes beside the verdict
        assert scoped["measured"]["requests"] == 10
        assert scoped["measured"]["errors"] == 0
        assert scoped["verdict"] == "met"

        unscoped = _by_name(data, "everything")
        assert unscoped["route"] is None
        assert unscoped["measured"]["requests"] == 15  # both surfaces
        assert unscoped["measured"]["errors"] == 2
        assert unscoped["verdict"] == "breached"  # 2/15 > the 5% bar
        break


async def test_route_scoped_budget_counts_the_same_rows(tmp_path):
    """The error-budget lens uses the SAME scope as the verdict (both
    query legs filter identically): the scoped target's budget is
    computed over the tool-server rows only, so a budget and its
    verdict can never disagree about what counts."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="tool-budget",
                    model="jira",
                    route="mcp",
                    metric="latency_p95_ms",
                    target=150.0,
                    min_requests=5,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        async with get_sessionmaker()() as session:
            _seed_routed(
                session,
                route="mcp",
                model="jira",
                rows=[{"status": "success", "latency_ms": 500} for _ in range(8)],
            )
            _seed_routed(
                session,
                route="chat",
                model="jira",
                rows=[{"status": "success", "latency_ms": 50} for _ in range(8)],
            )
            await session.commit()

        row = _by_name((await _slos(env.client)).json()["data"], "tool-budget")
        assert row["measured"]["latency_p95_ms"] == 500  # mcp rows only
        assert row["verdict"] == "breached"  # 500 against a 150 bar
        assert row["budget"]["measured"] == 8  # the SAME rows, not 16
        assert row["budget"]["bad"] == 8  # every mcp row is over the bar
        assert row["budget"]["burn_rate"] == 8 / 8 / 0.05  # observed / allowed
        break


async def test_route_scoped_ttft_is_honestly_unmeasurable(tmp_path):
    """A TTFT target over the MCP route has nothing to measure — MCP
    answers are complete JSON-RPC frames, nothing streams — and the
    verdict says so (insufficient_evidence, "nothing streamed"),
    never a fabricated number."""
    config = make_config(
        slos=SLOsConfig(
            targets=[
                _target(
                    name="tool-ttft",
                    model="jira",
                    route="mcp",
                    metric="ttft_p95_ms",
                    target=800.0,
                    min_requests=5,
                )
            ]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        async with get_sessionmaker()() as session:
            _seed_routed(
                session,
                route="mcp",
                model="jira",
                rows=[{"status": "success", "latency_ms": 120} for _ in range(9)],
            )
            await session.commit()

        row = _by_name((await _slos(env.client)).json()["data"], "tool-ttft")
        assert row["measured"]["requests"] == 9  # volume is real...
        assert row["measured"]["ttft_p95_ms"] is None  # ...the metric is not
        assert row["verdict"] == "insufficient_evidence"
        assert "nothing streamed" in row["reason"]
        break
