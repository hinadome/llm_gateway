"""Migration verdicts — tier 37: the go/no-go recommendation engine.

evaluate() is a PURE function over one evidence group — the same
shape shadow_diff.aggregate produces per mirrored deployment and
trials.run_summaries produces per trial run — so both evidence legs
verdict through one code path. What these tests pin, in one paragraph:
a clean full-volume group recommends go with every check's raw
numbers attached (auditable, never taken on faith); a failed match
rate or shadow-error rate is no_go (answers diverge, or the
candidate fails the traffic it is given); a marginal similarity mean
or a latency regression is only a hold (business calls, not
correctness bars) and never outranks a no_go; unmeasurable checks
report null and are skipped, never faked. Evidence sufficiency gates
the verdict on two volumes at once — total pairs (what an error rate
can be measured on) and comparable pairs (what answer equivalence can
be judged on) — with one deliberate exception, pinned here: a
shadow-error rate over its bar on a full volume of pairs is a no_go
even when nothing was comparable, because a candidate that fails every
call it is given has already answered the question.

The e2e side drives both surfaces: mirror evidence through the
four-twin fan-out (exact / divergent / failing / slower — one verdict
per check), GET /admin/migration/verdicts with its filters, threshold
echo and the standing-scope exclusion of trial rows, and trial
evidence through the run rows' folded recommendations — including the
honesty pin that a not_comparable-only run (rewritten primaries, so
raw-vs-raw is impossible) is insufficient_evidence, never a fake go.
"""

from __future__ import annotations

from gateway.config import (
    DeploymentConfig,
    GatewayConfig,
    MigrationConfig,
    ModelConfig,
)
from gateway.core.migration import (
    RECOMMENDATION_GO,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_INSUFFICIENT,
    RECOMMENDATION_NO_GO,
    evaluate,
)

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_trials import _chat, _drain, _start_trial, _trial_detail

DRIFTED_ANSWER = "The candidate provider absolutely refuses to answer."


def _evidence(**over) -> dict:
    """One evidence group — the shape both surfaces produce — with a
    clean full-volume baseline (25 pairs, 24 exact + 1 near, equal
    latency means) that each unit overrides at the corner it pins."""
    base = {
        "pairs": 25,
        "exact": 24,
        "near": 1,
        "divergent": 0,
        "shadow_error": 0,
        "not_comparable": 0,
        "comparable": 25,
        "match_rate": 1.0,
        "similarity_mean": 0.99,
        "shadow_latency_ms": 100,
        "primary_latency_ms": 100,
    }
    return {**base, **over}


def _mirror_config(migration: MigrationConfig | None = None) -> GatewayConfig:
    """test-model served by openai/primary with FOUR shadow twins —
    exact (same mock content), divergent (per-model override),
    unstable (scripted failures) and slow (per-model delay) — so one
    request fans out into four differently-verdicted groups. The
    default latency bar is effectively unbounded: the pins are the
    quality verdicts and that the slow twin's regression is MEASURED
    (the hold itself is pinned separately, with a real bar)."""
    return make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/primary", provider="openai", model="model-a"
                    ),
                    DeploymentConfig(
                        name="openai/twin",
                        provider="openai",
                        model="model-shadow",
                        shadow=True,
                    ),
                    DeploymentConfig(
                        name="openai/drifter",
                        provider="openai",
                        model="model-b",
                        shadow=True,
                    ),
                    DeploymentConfig(
                        name="openai/unstable",
                        provider="openai",
                        model="model-c",
                        shadow=True,
                    ),
                    DeploymentConfig(
                        name="openai/slow",
                        provider="openai",
                        model="model-d",
                        shadow=True,
                    ),
                ],
            )
        ],
        pricing={
            "model-a": {"input": 2.0, "output": 4.0},
            "model-shadow": {"input": 2.0, "output": 4.0},
            "model-b": {"input": 2.0, "output": 4.0},
            "model-c": {"input": 2.0, "output": 4.0},
            "model-d": {"input": 2.0, "output": 4.0},
        },
        migration=migration or MigrationConfig(min_pairs=1, max_latency_delta_pct=10_000.0),
    )


def _trial_config(
    migration: MigrationConfig | None = None,
) -> GatewayConfig:
    """The tier-36 trial shape (candidate on another public model)
    with the migration thresholds pinned low for small captures."""
    return make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/primary", provider="openai", model="model-a"
                    )
                ],
            ),
            ModelConfig(
                name="other-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/candidate", provider="openai", model="model-b"
                    )
                ],
            ),
        ],
        pricing={
            "model-a": {"input": 2.0, "output": 4.0},
            "model-b": {"input": 2.0, "output": 4.0},
        },
        migration=migration or MigrationConfig(min_pairs=1),
    )


async def _verdict_rows(client, **params) -> dict:
    resp = await client.get(
        "/admin/migration/verdicts", params=params, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ----------------------------------------------------------------------
# Units: the pure recommendation engine
# ----------------------------------------------------------------------


def test_clean_evidence_recommends_go():
    """A full-volume clean group is a go with nothing to explain: all
    five checks measured and passed, reasons empty — the checks speak."""
    rec = evaluate(_evidence(), MigrationConfig())
    assert rec["verdict"] == RECOMMENDATION_GO
    assert rec["reasons"] == []
    by_name = {c["name"]: c for c in rec["checks"]}
    assert set(by_name) == {
        "shadow_error_rate",
        "match_rate",
        "similarity_mean",
        "latency_delta_pct",
        "comparable_pairs",
    }
    assert all(c["pass"] is True for c in rec["checks"])
    # The audit shape: every check carries value + threshold + pass.
    assert by_name["shadow_error_rate"] == {
        "name": "shadow_error_rate",
        "value": 0.0,
        "threshold": "<= 0.02",
        "pass": True,
    }
    assert by_name["latency_delta_pct"]["value"] == 0.0


def test_match_rate_failure_is_no_go():
    """Answers that diverge are disqualifying: 2 of 25 comparable
    answers diverged, the match rate misses its bar, verdict no_go."""
    rec = evaluate(
        _evidence(exact=21, near=2, divergent=2, match_rate=0.92),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_NO_GO
    by_name = {c["name"]: c for c in rec["checks"]}
    assert by_name["match_rate"]["pass"] is False
    assert any("match rate" in r for r in rec["reasons"])


def test_shadow_error_failure_is_no_go():
    """A candidate that fails 2 of 25 mirrored calls misses the
    shadow-error bar — a no_go however good the comparable answers
    look (the failing calls are 2 pairs that will never be exact)."""
    rec = evaluate(
        _evidence(exact=23, near=0, shadow_error=2, comparable=23),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_NO_GO
    assert rec["checks"][0]["name"] == "shadow_error_rate"
    assert rec["checks"][0]["pass"] is False
    assert "shadow_error" in rec["reasons"][0]


def test_all_errors_on_thin_volume_is_insufficient():
    """3 pairs, all failed: a bad five-minute window is not a verdict —
    insufficient_evidence, with the failure still surfaced as a
    reason (an operator can read the writing on the wall)."""
    rec = evaluate(
        _evidence(
            pairs=3,
            exact=0,
            near=0,
            divergent=0,
            shadow_error=3,
            comparable=0,
            match_rate=None,
            similarity_mean=None,
            shadow_latency_ms=None,
            primary_latency_ms=None,
        ),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_INSUFFICIENT
    assert "only 3 pair(s)" in rec["reasons"][0]
    assert any("shadow_error" in r for r in rec["reasons"])


def test_all_errors_on_full_volume_is_no_go():
    """The escape: 25 pairs, every one failed — a full volume of
    evidence that the candidate cannot serve the traffic. no_go, with
    the error failure leading the reasons — not downgraded to
    insufficient by the (empty) comparable leg."""
    rec = evaluate(
        _evidence(
            pairs=25,
            exact=0,
            near=0,
            divergent=0,
            shadow_error=25,
            comparable=0,
            match_rate=None,
            similarity_mean=None,
            shadow_latency_ms=None,
            primary_latency_ms=None,
        ),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_NO_GO
    assert "shadow_error" in rec["reasons"][0]
    assert not any("min_pairs" in r for r in rec["reasons"])


def test_low_similarity_mean_is_hold():
    """All answers match, but cosmetically drifted on average: a hold
    (the operator decides whether the drift is acceptable), not a
    no_go — nothing diverged."""
    rec = evaluate(_evidence(similarity_mean=0.85), MigrationConfig())
    assert rec["verdict"] == RECOMMENDATION_HOLD
    assert any("similarity" in r for r in rec["reasons"])


def test_latency_regression_is_hold():
    """A candidate 40% slower on the mean is a hold: latency is a
    business trade measured as a PERCENT of the serving primary (the
    check value), never an absolute bar."""
    rec = evaluate(
        _evidence(shadow_latency_ms=140, primary_latency_ms=100),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_HOLD
    by_name = {c["name"]: c for c in rec["checks"]}
    assert by_name["latency_delta_pct"]["value"] == 40.0
    assert by_name["latency_delta_pct"]["pass"] is False
    assert any("latency" in r for r in rec["reasons"])


def test_hold_concerns_never_outrank_no_go():
    """Worst honest call wins: a failing match rate, a failing
    similarity mean AND a latency regression all fire, and the verdict
    is the no_go — every reason still surfaced."""
    rec = evaluate(
        _evidence(
            exact=15,
            near=6,
            divergent=4,
            match_rate=0.84,
            similarity_mean=0.72,
            shadow_latency_ms=200,
        ),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_NO_GO
    assert len(rec["reasons"]) == 3


def test_thin_comparable_leg_is_insufficient():
    """30 pairs but only 5 comparable (the rest not_comparable —
    tool-call replies and the like prove nothing about equivalence):
    the quality leg cannot be judged, whatever the observable
    numbers say (and here they say 100% match)."""
    rec = evaluate(
        _evidence(
            pairs=30,
            exact=5,
            near=0,
            divergent=0,
            not_comparable=25,
            comparable=5,
            match_rate=1.0,
            similarity_mean=1.0,
        ),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_INSUFFICIENT
    assert "only 5 of 30 pair(s) were comparable" in rec["reasons"][0]


def test_unmeasurable_checks_are_skipped_not_passed():
    """Missing latency means (a pruned primary, a bodies-off capture)
    make the latency check null-pass — skipped, never faked — and a
    zero primary mean is guarded the same way. The verdict stands on
    the checks that were measured."""
    rec = evaluate(
        _evidence(shadow_latency_ms=None, primary_latency_ms=None),
        MigrationConfig(),
    )
    assert rec["verdict"] == RECOMMENDATION_GO
    by_name = {c["name"]: c for c in rec["checks"]}
    assert by_name["latency_delta_pct"]["value"] is None
    assert by_name["latency_delta_pct"]["pass"] is None
    guarded = evaluate(_evidence(primary_latency_ms=0), MigrationConfig())
    assert guarded["verdict"] == RECOMMENDATION_GO
    assert {c["name"]: c for c in guarded["checks"]}[
        "latency_delta_pct"
    ]["pass"] is None


def test_empty_evidence_is_insufficient():
    """An empty group (a pending run, an empty window) is honest: the
    volume note leads, and the rate checks are null — not a fake 0.0
    that would read as a perfect candidate."""
    rec = evaluate({}, MigrationConfig())
    assert rec["verdict"] == RECOMMENDATION_INSUFFICIENT
    assert "only 0 pair(s)" in rec["reasons"][0]
    by_name = {c["name"]: c for c in rec["checks"]}
    assert by_name["shadow_error_rate"]["value"] is None
    assert by_name["shadow_error_rate"]["pass"] is None
    assert by_name["match_rate"]["pass"] is None


# ----------------------------------------------------------------------
# e2e: mirror evidence — GET /admin/migration/verdicts
# ----------------------------------------------------------------------


async def test_verdicts_over_mirror_evidence(tmp_path):
    """One request fans out to four shadow twins and the endpoint
    verdicts each: the exact twin go, the divergent twin no_go (match
    rate), the failing twin no_go via the full-volume error escape,
    the slower twin's regression MEASURED in its check. Thresholds are
    echoed so every verdict is auditable against its bars."""
    async for env in _make_env(tmp_path, config=_mirror_config()):
        # Stabilize the latency leg: every call sleeps, so the int-ms
        # means never round to an unmeasurable 0 in-process.
        env.mock.delay_seconds = 0.05
        env.mock.model_content["model-b"] = DRIFTED_ANSWER
        env.mock.fail_counts["model-c"] = 99  # every mirror call fails
        env.mock.model_delays["model-d"] = 0.15
        for content in ("first request", "second request"):
            await _chat(env.client, content=content)
        await _drain(env)

        page = await _verdict_rows(env.client)
        # The live migration: thresholds are echoed, never assumed.
        assert page["thresholds"] == {
            "min_pairs": 1,
            "min_match_rate": 0.95,
            "max_shadow_error_rate": 0.02,
            "min_similarity_mean": 0.9,
            "max_latency_delta_pct": 10_000.0,
        }
        by_dep = {row["deployment"]: row for row in page["data"]}
        assert set(by_dep) == {
            "openai/twin",
            "openai/drifter",
            "openai/unstable",
            "openai/slow",
        }

        twin = by_dep["openai/twin"]
        assert (twin["pairs"], twin["exact"]) == (2, 2)
        rec = twin["recommendation"]
        assert rec["verdict"] == RECOMMENDATION_GO
        assert rec["reasons"] == []
        assert all(c["pass"] is True for c in rec["checks"])

        drift = by_dep["openai/drifter"]
        assert drift["divergent"] == 2
        rec = drift["recommendation"]
        assert rec["verdict"] == RECOMMENDATION_NO_GO
        assert any("match rate" in r for r in rec["reasons"])

        unstable = by_dep["openai/unstable"]
        assert (unstable["shadow_error"], unstable["comparable"]) == (2, 0)
        rec = unstable["recommendation"]
        assert rec["verdict"] == RECOMMENDATION_NO_GO  # the escape
        assert "shadow_error" in rec["reasons"][0]

        slow = by_dep["openai/slow"]
        rec = slow["recommendation"]
        # The regression is measured as a percent of the primary's
        # mean (an absolute ms bar would mean different things on a
        # 200ms and a 20s model) — well over the default 25% bar.
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["latency_delta_pct"]["value"] > 100.0


async def test_slow_twin_holds_at_the_real_latency_bar(tmp_path):
    """With the default 25% latency bar and a stabilized base latency
    (the mock sleeps on every call), the slower candidate is a hold —
    pinned separately from the fan-out test so the verdict depends on
    the real bar, not an unbounded one."""
    async for env in _make_env(
        tmp_path, config=_mirror_config(migration=MigrationConfig(min_pairs=1))
    ):
        env.mock.delay_seconds = 0.05
        env.mock.model_delays["model-d"] = 0.15
        for content in ("first request", "second request"):
            await _chat(env.client, content=content)
        await _drain(env)

        page = await _verdict_rows(env.client, deployment="openai/slow")
        rec = page["data"][0]["recommendation"]
        assert rec["verdict"] == RECOMMENDATION_HOLD
        assert any("latency" in r for r in rec["reasons"])


async def test_verdict_filters_and_trial_exclusion(tmp_path):
    """?model= / ?deployment= narrow the verdicts; a malformed since
    is the standard 400; and a candidate trial against the SAME
    deployment the mirrors already report leaves the standing verdict
    row byte-identical — trial rows are an experiment, not live
    mirroring."""
    async for env in _make_env(tmp_path, config=_mirror_config()):
        for content in ("first request", "second request"):
            await _chat(env.client, content=content)
        await _drain(env)

        one = await _verdict_rows(env.client, deployment="openai/twin")
        assert [row["deployment"] for row in one["data"]] == ["openai/twin"]
        all_four = await _verdict_rows(env.client, model="test-model")
        assert len(all_four["data"]) == 4
        # A deployment that never mirrors (the serving primary) has no
        # group — empty, not an error.
        empty = await _verdict_rows(env.client, deployment="openai/primary")
        assert empty["data"] == []
        resp = await env.client.get(
            "/admin/migration/verdicts",
            params={"since": "not-a-date"},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 400

        before = (await _verdict_rows(env.client, deployment="openai/twin"))["data"][
            0
        ]
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/twin"
        )
        await _drain(env)
        after = (await _verdict_rows(env.client, deployment="openai/twin"))["data"][
            0
        ]
        assert after == before
        # ...while the trial itself did run and did score pairs.
        detail = await _trial_detail(env.client, trial["id"])
        assert detail["summary"]["exact"] == 2


async def test_verdicts_are_platform_admin_only(tmp_path):
    """Verdicts read the same cross-tenant evidence as
    /admin/shadow-diff (the groups summarize bodies) — tenant admins
    and data-plane keys get 403."""
    async for env in _make_env(tmp_path, config=_mirror_config()):
        tenant = (
            await env.client.post(
                "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        tadmin = (
            await create_key(
                env.client, "tadmin", is_admin=True, tenant_id=tenant["id"]
            )
        )["key"]
        plain = (await create_key(env.client, "plain"))["key"]
        for key in (tadmin, plain):
            resp = await env.client.get(
                "/admin/migration/verdicts", headers=auth(key)
            )
            assert resp.status_code == 403, (key, resp.status_code)


# ----------------------------------------------------------------------
# e2e: trial evidence — recommendations folded into the run rows
# ----------------------------------------------------------------------


async def test_trial_run_recommends_go(tmp_path):
    """The trial's folded summary is a full evidence group — verdict
    counts, match rate, similarity mean AND the latency means against
    the original primaries (shadow_diff.trial_baselines) — so the
    run's recommendation is the same go a mirrored deployment with
    this evidence would get, through the same evaluate()."""
    config = _trial_config(MigrationConfig(min_pairs=1, max_latency_delta_pct=10_000.0))
    async for env in _make_env(tmp_path, config=config):
        # Stabilize the latency leg: every call (the captured primaries
        # AND the trial's replays) sleeps, so the int-ms means never
        # round to an unmeasurable 0 in-process.
        env.mock.delay_seconds = 0.05
        for content in ("first capture", "second capture"):
            await _chat(env.client, content=content)
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["summary"]["pairs"] == 2
        assert detail["summary"]["comparable"] == 2
        assert detail["summary"]["match_rate"] == 1.0
        rec = detail["recommendation"]
        assert rec["verdict"] == RECOMMENDATION_GO
        assert rec["reasons"] == []
        by_name = {c["name"]: c for c in rec["checks"]}
        # The latency leg is measured: the trial's candidate means are
        # joined back to the captured primaries' recorded latencies.
        assert by_name["latency_delta_pct"]["value"] is not None
        assert by_name["latency_delta_pct"]["pass"] is True

        listing = (
            await env.client.get("/admin/trials", headers=auth(ADMIN_KEY))
        ).json()
        assert listing["data"][0]["recommendation"]["verdict"] == RECOMMENDATION_GO


async def test_not_comparable_trial_is_insufficient(tmp_path):
    """The honesty pin: a run whose only pair is not_comparable (the
    primary was rewritten by a response guardrail, so the stored body
    is the client-seen text and raw-vs-raw is impossible) cannot judge
    equivalence — insufficient_evidence, never a fake go on an
    unmeasurable quality leg."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        await _chat(env.client, content="PIIPLEASE")
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["summary"]["not_comparable"] == 1
        assert detail["summary"]["comparable"] == 0
        rec = detail["recommendation"]
        assert rec["verdict"] == RECOMMENDATION_INSUFFICIENT
        assert "comparable" in rec["reasons"][0]
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["match_rate"]["pass"] is None  # skipped, not passed

        listing = (
            await env.client.get("/admin/trials", headers=auth(ADMIN_KEY))
        ).json()
        assert (
            listing["data"][0]["recommendation"]["verdict"]
            == RECOMMENDATION_INSUFFICIENT
        )
