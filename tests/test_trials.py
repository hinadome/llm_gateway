"""Tests for candidate trials (tier 36): deliberate replay of a
captured traffic window against a named candidate deployment.

The contract pinned here, in one paragraph: POST /admin/trials selects
one public model's successful, body-storing chat rows (a captured
window), replays each against the candidate with REAL upstream calls —
throttled into waves of ``concurrency`` and capped at ``max_cost_usd``
(clamped to the ``trials:`` ceilings) — and writes shadow log rows that
score with the tier-35 verdict vocabulary. Trial execution is
mirror-like: no guardrails/counters/budgets/cache, no breaker
bookkeeping, no key spend. Scoring is honest about the persistence
boundary: a primary rewritten by a response/stream guardrail scores
``not_comparable`` (its stored body is the client-seen text, and
raw-vs-raw is impossible). The standing shadow-diff scope excludes
trial rows; /admin/trials/{id}/pairs is their drilldown. Status
transitions are guarded DB updates — cancel wins within one wave from
any replica, and the startup sweep orphans nothing forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from gateway.config import (
    DeploymentConfig,
    GatewayConfig,
    ModelConfig,
    TrialsConfig,
)
from gateway.core.shadow_diff import (
    VERDICT_DIVERGENT,
    VERDICT_EXACT,
    VERDICT_NOT_COMPARABLE,
    VERDICT_SHADOW_ERROR,
    extract_content_text,
)
from gateway.core.trials import (
    STOP_CANCELLED,
    STOP_COST_CAP,
    STOP_DONE,
    STOP_NO_ENTRIES,
    STOP_ORPHANED,
    clamp_params,
    run_trial,
    sweep_stale,
)
from gateway.db import get_sessionmaker
from gateway.models import RequestLog, Trial

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_shadow_diff import _wait_for_result

MOCK_ANSWER = "Hello from the mock!"
DRIFTED_ANSWER = "The candidate provider absolutely refuses to answer."
# The mock reports 10 prompt + 7 completion tokens; the test config
# prices model-b at $2/M input + $4/M output.
PER_CALL_COST = (10 * 2.0 + 7 * 4.0) / 1_000_000


def _trial_config(shadow_twin: bool = False) -> GatewayConfig:
    """test-model served by openai/primary (model-a). The CANDIDATE
    (openai/candidate, model-b) deliberately belongs to a DIFFERENT
    public model — proving a candidate need not be one of the replayed
    model's deployments. ``shadow_twin`` adds a mirroring twin for the
    standing-aggregate exclusion test."""
    twin = (
        [
            DeploymentConfig(
                name="openai/twin",
                provider="openai",
                model="model-shadow",
                shadow=True,
            )
        ]
        if shadow_twin
        else []
    )
    return make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/primary", provider="openai", model="model-a"
                    ),
                    *twin,
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
            "model-shadow": {"input": 2.0, "output": 4.0},
        },
    )


async def _chat(client, key: str = ADMIN_KEY, **body) -> str:
    resp = await client.post(
        "/v1/chat/completions", json=chat_body(**body), headers=auth(key)
    )
    assert resp.status_code == 200, resp.text
    return resp.headers["x-request-id"]


async def _drain(env) -> None:
    """Wait out every tracked background task (mirrors, spend
    accounting, the trial run itself) — the deterministic equivalent
    of the lifespan loops ASGITransport never starts."""
    await env.app.state.pipeline.drain_background_tasks()


async def _start_trial(client, **fields) -> dict:
    resp = await client.post("/admin/trials", json=fields, headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _trial_detail(client, trial_id: str) -> dict:
    resp = await client.get(f"/admin/trials/{trial_id}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _trial_pairs(client, trial_id: str, **params) -> dict:
    resp = await client.get(
        f"/admin/trials/{trial_id}/pairs", params=params, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows_for(client, request_id: str) -> list[dict]:
    resp = await client.get(
        "/admin/logs", params={"request_id": request_id}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _trial_rows(client, trial_id: str) -> list[dict]:
    """All log rows belonging to one trial run."""
    resp = await client.get("/admin/logs", params={"limit": 500}, headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return [r for r in resp.json()["data"] if r.get("trial_id") == trial_id]


# ----------------------------------------------------------------------
# Config + clamping
# ----------------------------------------------------------------------


def test_clamp_params_units():
    """Missing values fall back to the ceilings; explicit values pass
    through below them and clamp at them — the platform's trials:
    block is the hard bound, whatever the request asked for."""
    defaults = clamp_params(TrialsConfig())
    assert defaults == {
        "since": None,
        "until": None,
        "limit": 200,
        "concurrency": 2,
        "max_cost_usd": 1.0,
    }
    kept = clamp_params(
        TrialsConfig(), limit=5, concurrency=1, max_cost_usd=0.25
    )
    assert (kept["limit"], kept["concurrency"], kept["max_cost_usd"]) == (5, 1, 0.25)
    clamped = clamp_params(
        TrialsConfig(), limit=99999, concurrency=99, max_cost_usd=99.0
    )
    assert (clamped["limit"], clamped["concurrency"], clamped["max_cost_usd"]) == (
        200,
        2,
        1.0,
    )
    tight = clamp_params(
        TrialsConfig(max_entries=3, max_concurrency=1, max_cost_usd=0.01),
        limit=50,
        concurrency=8,
        max_cost_usd=5.0,
    )
    assert (tight["limit"], tight["concurrency"], tight["max_cost_usd"]) == (3, 1, 0.01)


# ----------------------------------------------------------------------
# The e2e run
# ----------------------------------------------------------------------


async def test_trial_run_replays_and_scores(tmp_path):
    """The happy path end to end: three captured requests replay against
    the candidate, each pair scores exact, the run row carries honest
    progress/cost, and the drilldown shows both sides."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        ids = [
            await _chat(env.client, content=c)
            for c in ("first capture", "second capture", "third capture")
        ]
        trial = await _start_trial(
            env.client,
            model="test-model",
            deployment="openai/candidate",
            limit=10,
            concurrency=2,
        )
        assert trial["status"] == "pending"
        assert trial["params"]["limit"] == 10  # the clamped truth is stored
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["status"] == "completed"
        assert detail["stop_reason"] == STOP_DONE
        assert detail["selected"] == 3
        assert detail["completed"] == 3
        assert detail["skipped"] == 0
        assert detail["cost_usd"] >= 3 * PER_CALL_COST
        # Tier 37: the summary is a full migration evidence group —
        # the same shape a shadow-diff mirror group carries — so the
        # run verdicts through core/migration.evaluate like one. The
        # latency means are honest int-ms values (in-process mock
        # calls may round below 1ms; the verdict's latency check
        # skips on a 0 primary mean, never fakes a percent), so the
        # == pin covers the deterministic fields and the latency leg
        # its shape + internal consistency.
        summary = detail["summary"]
        assert {
            k: v for k, v in summary.items() if "latency" not in k
        } == {
            "pairs": 3,
            "exact": 3,
            "near": 0,
            "divergent": 0,
            "shadow_error": 0,
            "not_comparable": 0,
            "comparable": 3,
            "match_rate": 1.0,
            "similarity_mean": 1.0,
        }
        assert all(
            isinstance(summary[k], int)
            for k in ("shadow_latency_ms", "primary_latency_ms", "latency_delta_ms")
        )
        assert (
            summary["latency_delta_ms"]
            == summary["shadow_latency_ms"] - summary["primary_latency_ms"]
        )

        # One trial row per replayed entry, sharing the original's
        # request_id, flagged shadow, scored, on the candidate.
        for rid in ids:
            trows = [
                r
                for r in await _rows_for(env.client, rid)
                if r.get("trial_id") == trial["id"]
            ]
            assert len(trows) == 1
            row = trows[0]
            assert row["shadow"] is True
            assert row["model"] == "test-model"
            assert row["deployment"] == "openai/candidate"
            assert row["status"] == "success"
            assert row["shadow_verdict"] == VERDICT_EXACT
            assert row["shadow_similarity"] == 1.0
            assert row["cost_usd"] and row["cost_usd"] > 0

        page = await _trial_pairs(env.client, trial["id"])
        assert page["total"] == 3
        pair = page["data"][0]
        assert pair["verdict"] == VERDICT_EXACT
        assert pair["primary"]["text"] == MOCK_ANSWER
        assert pair["shadow"]["text"] == MOCK_ANSWER


async def test_divergent_candidate_and_verdict_filter(tmp_path):
    """A candidate scripted to answer differently scores divergent, the
    run summary says so, and the drilldown's verdict filter is the
    review queue."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        env.mock.model_content["model-b"] = DRIFTED_ANSWER
        await _chat(env.client, content="one")
        await _chat(env.client, content="two")
        trial = await _start_trial(env.client, model="test-model", deployment="openai/candidate")
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["summary"]["divergent"] == 2
        assert detail["summary"]["exact"] == 0
        assert detail["summary"]["similarity_mean"] < 0.8

        queue = await _trial_pairs(env.client, trial["id"], verdict="divergent")
        assert queue["total"] == 2
        assert all(p["verdict"] == VERDICT_DIVERGENT for p in queue["data"])
        none = await _trial_pairs(env.client, trial["id"], verdict="exact")
        assert none["total"] == 0
        # the filter validates its vocabulary like the shadow-diff one
        bad = await env.client.get(
            f"/admin/trials/{trial['id']}/pairs",
            params={"verdict": "bogus"},
            headers=auth(ADMIN_KEY),
        )
        assert bad.status_code == 400
        assert "verdict" in bad.json()["error"]["message"]


async def test_rewritten_primary_scores_not_comparable(tmp_path):
    """Scoring honesty: the stored primary body is the CLIENT-SEEN text
    (the raw provider answer is not persisted), so a primary rewritten
    by a response-phase guardrail — the PII redactor masks the email —
    must score not_comparable against the candidate's raw answer, never
    a fake divergence."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        rid = await _chat(env.client, content="PIIPLEASE")
        rows = await _rows_for(env.client, rid)
        primary = next(r for r in rows if not r["shadow"])
        # The rewrite really happened: the stored body is masked and
        # the guardrail actions record the response-phase modify — this
        # is what makes the honesty check load-bearing here.
        detail = (
            await env.client.get(
                f"/admin/logs/{primary['id']}", headers=auth(ADMIN_KEY)
            )
        ).json()
        stored_text = extract_content_text(detail["response_body"])
        assert stored_text is not None and "bot@example.com" not in stored_text
        assert any(
            a.get("phase") == "response" and a.get("action") == "modify"
            for a in detail["guardrail_actions"]
        )

        trial = await _start_trial(env.client, model="test-model", deployment="openai/candidate")
        await _drain(env)

        trows = await _trial_rows(env.client, trial["id"])
        assert len(trows) == 1
        assert trows[0]["shadow_verdict"] == VERDICT_NOT_COMPARABLE
        assert trows[0]["shadow_similarity"] is None
        # and the row is honest about the candidate's raw answer
        tdetail = (
            await env.client.get(
                f"/admin/logs/{trows[0]['id']}", headers=auth(ADMIN_KEY)
            )
        ).json()
        assert "bot@example.com" in (extract_content_text(tdetail["response_body"]) or "")
        summary = (await _trial_detail(env.client, trial["id"]))["summary"]
        assert summary["not_comparable"] == 1
        assert summary["similarity_mean"] is None


# ----------------------------------------------------------------------
# Caps: cost, concurrency, cancel
# ----------------------------------------------------------------------


async def test_cost_cap_stops_before_next_wave(tmp_path):
    """A cap below one call's cost lets the FIRST wave run (the cap is
    a between-wave cap — it can never abort in-flight calls) and then
    refuses every next one."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        for content in ("one", "two", "three"):
            await _chat(env.client, content=content)
        trial = await _start_trial(
            env.client,
            model="test-model",
            deployment="openai/candidate",
            concurrency=1,
            max_cost_usd=PER_CALL_COST / 4,
        )
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["status"] == "completed"  # the cap is a normal end
        assert detail["stop_reason"] == STOP_COST_CAP
        assert detail["selected"] == 3
        assert detail["completed"] == 1  # wave 1 ran; waves 2+ were refused
        assert detail["cost_usd"] >= PER_CALL_COST
        assert len(await _trial_rows(env.client, trial["id"])) == 1


async def test_concurrency_cap_bounds_waves(tmp_path):
    """Waves run at most `concurrency` calls in flight — the mock's
    high-water mark proves both the bound and that waves really are
    parallel."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        env.mock.delay_seconds = 0.05
        try:
            for content in ("a", "b", "c", "d", "e", "f"):
                await _chat(env.client, content=content)
            trial = await _start_trial(
                env.client,
                model="test-model",
                deployment="openai/candidate",
                concurrency=2,
                limit=10,
            )
            await _drain(env)
        finally:
            env.mock.delay_seconds = 0.0

        assert env.mock.max_inflight == 2  # 1 during the chats, 2 per wave
        detail = await _trial_detail(env.client, trial["id"])
        assert detail["completed"] == 6
        assert detail["status"] == "completed"


async def test_cancel_mid_run_wins_within_one_wave(tmp_path):
    """A cancel from the admin API lands between waves (DB status is
    the truth): the in-flight wave finishes and is counted, every next
    wave is refused, and the row's final state belongs to the cancel."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        env.mock.delay_seconds = 0.15
        try:
            for content in ("slow one", "slow two", "slow three"):
                await _chat(env.client, content=content)
            trial = await _start_trial(
                env.client,
                model="test-model",
                deployment="openai/candidate",
                concurrency=1,
            )
            # wait until the claim has landed and wave 1 is in flight
            async def _running():
                d = await _trial_detail(env.client, trial["id"])
                return d if d["status"] == "running" else None

            await _wait_for_result(_running, timeout=5.0, what="trial to claim")
            cancelled = (
                await env.client.post(
                    f"/admin/trials/{trial['id']}/cancel", headers=auth(ADMIN_KEY)
                )
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            await _drain(env)
        finally:
            env.mock.delay_seconds = 0.0

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["status"] == "cancelled"
        assert detail["stop_reason"] == STOP_CANCELLED
        assert detail["selected"] == 3
        assert detail["completed"] == 1  # wave 1 finished under the cancel
        assert len(await _trial_rows(env.client, trial["id"])) == 1
        # a finished run is not cancellable again
        again = await env.client.post(
            f"/admin/trials/{trial['id']}/cancel", headers=auth(ADMIN_KEY)
        )
        assert again.status_code == 409


# ----------------------------------------------------------------------
# Selection: what replays, what doesn't
# ----------------------------------------------------------------------


async def test_selection_replays_only_replayable_primaries(tmp_path):
    """Only successful, body-storing, client-facing CHAT rows of the
    model replay: error rows have no answer to compare, embeddings are
    another surface, mirror rows are not primaries."""
    async for env in _make_env(tmp_path, config=_trial_config(shadow_twin=True)):
        env.mock.fail_counts["model-a"] = 99
        failed = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="this one will fail"),
            headers=auth(ADMIN_KEY),
        )
        assert failed.status_code >= 500
        env.mock.fail_counts["model-a"] = 0
        good_ids = [
            await _chat(env.client, content="good one"),
            await _chat(env.client, content="good two"),
        ]
        embeddings = await env.client.post(
            "/v1/embeddings",
            json={"model": "test-model", "input": "embed me"},
            headers=auth(ADMIN_KEY),
        )
        assert embeddings.status_code == 200
        await _drain(env)  # the twin's mirror rows land too

        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate", limit=10
        )
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["selected"] == 2
        assert detail["completed"] == 2
        trial_ids = {r["request_id"] for r in await _trial_rows(env.client, trial["id"])}
        assert trial_ids == set(good_ids)  # the error/embedding/mirror rows stay out


async def test_unparseable_stored_body_counts_skipped(tmp_path):
    """A capture whose stored body no longer parses as a chat request
    is a skip, not a crash and not a row."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        await _chat(env.client, content="real capture")
        async with get_sessionmaker()() as session:
            session.add(
                RequestLog(
                    request_id="req_bogus_capture",
                    key_name="test-key",
                    model="test-model",
                    route="chat",
                    status="success",
                    shadow=False,
                    request_body={"model": "test-model", "messages": "not-a-list"},
                    response_body={
                        "choices": [{"message": {"content": MOCK_ANSWER}}]
                    },
                )
            )
            await session.commit()
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["selected"] == 2
        assert detail["completed"] == 1
        assert detail["skipped"] == 1
        rows = await _rows_for(env.client, "req_bogus_capture")
        assert all(r.get("trial_id") is None for r in rows)  # no row for the skip


async def test_empty_window_completes_with_no_entries(tmp_path):
    """A window that captured nothing is a normal completion with an
    honest stop reason — not an error."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        await _chat(env.client, content="before the window")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        trial = await _start_trial(
            env.client,
            model="test-model",
            deployment="openai/candidate",
            since=future,
        )
        await _drain(env)
        detail = await _trial_detail(env.client, trial["id"])
        assert detail["status"] == "completed"
        assert detail["stop_reason"] == STOP_NO_ENTRIES
        assert detail["selected"] == 0
        assert detail["completed"] == 0
        assert detail["cost_usd"] == 0


# ----------------------------------------------------------------------
# Mirror-like isolation
# ----------------------------------------------------------------------


async def test_trials_never_touch_spend_or_breakers(tmp_path):
    """Trial calls cost the CANDIDATE money but nobody's budget: key
    spend is untouched, and a candidate that fails every replay —
    shadow_error verdicts, the loudest finding — must not have its
    circuit breaker punished by the experiment."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        key = await create_key(env.client, "spendkey")
        await _chat(env.client, key=key["key"], content="chargeable")
        await _drain(env)
        keys = (
            (await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))).json()
        )["data"]
        spend_before = next(k for k in keys if k["id"] == key["id"])["spend_usd"]
        assert spend_before > 0  # the primary leg billed the key

        env.mock.fail_counts["model-b"] = 99  # every candidate call fails
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)

        detail = await _trial_detail(env.client, trial["id"])
        assert detail["summary"]["shadow_error"] == 1
        assert detail["completed"] == 1
        keys = (
            (await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))).json()
        )["data"]
        assert (
            next(k for k in keys if k["id"] == key["id"])["spend_usd"] == spend_before
        )  # the trial rows cost the key nothing

        routing = (
            await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))
        ).json()
        candidate = next(
            d
            for m in routing["models"]
            for d in m["deployments"]
            if d["name"] == "openai/candidate"
        )
        assert candidate["breaker"]["state"] == "closed"
        assert candidate["breaker"]["consecutive_failures"] == 0


# ----------------------------------------------------------------------
# Scoping + access
# ----------------------------------------------------------------------


async def test_trial_rows_excluded_from_standing_shadow_diff(tmp_path):
    """A trial is an experiment, not live mirroring: the standing
    /admin/shadow-diff aggregate keeps counting only real mirror pairs
    (trial_id IS NULL), while the trial's own drilldown carries its
    rows. The list endpoint folds the verdict summary into each row."""
    async for env in _make_env(tmp_path, config=_trial_config(shadow_twin=True)):
        await _chat(env.client, content="mirrored and trialed one")
        await _chat(env.client, content="mirrored and trialed two")
        await _drain(env)  # the twin's mirrors land first
        standing_before = (
            await env.client.get("/admin/shadow-diff", headers=auth(ADMIN_KEY))
        ).json()

        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)

        standing_after = (
            await env.client.get("/admin/shadow-diff", headers=auth(ADMIN_KEY))
        ).json()
        # The twin's pairs are still there; the candidate never appears
        # as a mirror group, and the counts did not move.
        assert [g["deployment"] for g in standing_after["groups"]] == ["openai/twin"]
        assert standing_after == standing_before

        page = await _trial_pairs(env.client, trial["id"])
        assert page["total"] == 2
        assert all(p["verdict"] == VERDICT_EXACT for p in page["data"])

        listing = (
            await env.client.get("/admin/trials", headers=auth(ADMIN_KEY))
        ).json()
        assert listing["total"] == 1
        assert listing["data"][0]["summary"]["exact"] == 2  # folded in, no N+1


async def test_trials_are_platform_admin_only(tmp_path):
    """The pairs carry request bodies, which cross tenants — the same
    boundary as /admin/routing and /admin/shadow-diff. Tenant admins
    and data-plane keys get 403 on every trials endpoint."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        await _chat(env.client, content="captured")
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)

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
            for method, path in (
                ("get", "/admin/trials"),
                ("post", "/admin/trials"),
                ("get", f"/admin/trials/{trial['id']}"),
                ("get", f"/admin/trials/{trial['id']}/pairs"),
                ("post", f"/admin/trials/{trial['id']}/cancel"),
            ):
                kwargs = (
                    {
                        "json": {
                            "model": "test-model",
                            "deployment": "openai/candidate",
                        }
                    }
                    if path == "/admin/trials" and method == "post"
                    else {}
                )
                resp = await getattr(env.client, method)(
                    path, headers=auth(key), **kwargs
                )
                assert resp.status_code == 403, (method, path, resp.status_code)


# ----------------------------------------------------------------------
# Validation + lifecycle edges
# ----------------------------------------------------------------------


async def test_validation_and_disabled_surface(tmp_path):
    """Unknown model/deployment, malformed windows and a disabled
    surface refuse with the gateway's 400s (or the disabled 404);
    finished runs answer 409 on cancel, unknown ids 404."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        unknown_model = await env.client.post(
            "/admin/trials",
            json={"model": "no-such-model", "deployment": "openai/candidate"},
            headers=auth(ADMIN_KEY),
        )
        assert unknown_model.status_code == 400
        assert "no-such-model" in unknown_model.json()["error"]["message"]

        unknown_deployment = await env.client.post(
            "/admin/trials",
            json={"model": "test-model", "deployment": "openai/nope"},
            headers=auth(ADMIN_KEY),
        )
        assert unknown_deployment.status_code == 400
        assert "openai/nope" in unknown_deployment.json()["error"]["message"]

        bad_window = await env.client.post(
            "/admin/trials",
            json={
                "model": "test-model",
                "deployment": "openai/candidate",
                "since": "not-a-timestamp",
            },
            headers=auth(ADMIN_KEY),
        )
        assert bad_window.status_code == 400
        assert "since" in bad_window.json()["error"]["message"]

        await _chat(env.client, content="captured")
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)
        finished = await env.client.post(
            f"/admin/trials/{trial['id']}/cancel", headers=auth(ADMIN_KEY)
        )
        assert finished.status_code == 409

        missing = await env.client.get(
            "/admin/trials/trial_doesnotexist", headers=auth(ADMIN_KEY)
        )
        assert missing.status_code == 404
        missing_pairs = await env.client.get(
            "/admin/trials/trial_doesnotexist/pairs", headers=auth(ADMIN_KEY)
        )
        assert missing_pairs.status_code == 404

    async for env in _make_env(
        tmp_path, config=make_config(trials=TrialsConfig(enabled=False))
    ):
        await _chat(env.client, content="captured")
        disabled = await env.client.post(
            "/admin/trials",
            json={"model": "test-model", "deployment": "openai/fallback"},
            headers=auth(ADMIN_KEY),
        )
        assert disabled.status_code == 404
        assert "disabled" in disabled.json()["error"]["message"]


async def test_sweep_orphans_and_guarded_claim(tmp_path):
    """The startup sweep cancels runs a dead process left behind (and
    never touches finished ones); the runner's claim is guarded on
    status='pending', so a swept row can never be run afterwards."""
    async for env in _make_env(tmp_path, config=_trial_config()):
        async with get_sessionmaker()() as session:
            running = Trial(
                model="test-model", deployment="openai/candidate", params={}
            )
            pending = Trial(
                model="test-model", deployment="openai/candidate", params={}
            )
            done = Trial(
                model="test-model",
                deployment="openai/candidate",
                params={},
                status="completed",
                stop_reason="done",
            )
            session.add_all([running, pending, done])
            await session.commit()
            ids = [running.id, pending.id, done.id]

        swept = await sweep_stale(get_sessionmaker())
        assert swept == 2

        async with get_sessionmaker()() as session:
            rows = {
                t.id: t
                for t in (await session.scalars(select(Trial))).all()
            }
        assert rows[ids[0]].status == "cancelled"
        assert rows[ids[0]].stop_reason == STOP_ORPHANED
        assert rows[ids[1]].status == "cancelled"
        assert rows[ids[2]].status == "completed"  # finished rows untouched
        assert rows[ids[2]].stop_reason == "done"

        # The guarded claim: running a cancelled trial is a no-op.
        await run_trial(env.app.state.pipeline, ids[0])
        async with get_sessionmaker()() as session:
            row = await session.get(Trial, ids[0])
            assert row.status == "cancelled"
            assert row.completed == 0
