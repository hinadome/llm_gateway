"""Shadow diffing — tier 35: the evidence layer over mirrored traffic.

Shadow mirroring already writes paired rows (same request_id: the
client-facing primary, the mirrored candidate with its own body /
latency / cost). This tier scores each pair at mirror time and turns
the rows into migration evidence:

* **Raw-vs-raw** is the load-bearing decision: the primary's *raw
  provider text* (pre-guardrail, captured at queue time — the buffered
  path stashes the pre-phase response, the streaming path passes the
  accumulated source text) is diffed against the mirror's answer,
  which runs no guardrails. A rewrite on the primary leg is therefore
  NOT provider drift — pinned by the PII test, where the client-seen
  body is masked, the mirror's is raw, and the verdict is still exact.
* **Fixed buckets** (no config): exact (ratio 1.0), near (≥ 0.80),
  divergent, shadow_error (the mirror call failed — the loudest
  migration signal of all), not_comparable (no text on a side — never
  a fake 0.0). The raw similarity ships on every row so re-bucketing
  is possible after the fact.
* **Aggregation stays SQL**: verdict + similarity are stored on the
  shadow row, so /admin/shadow-diff is a GROUP BY over columns — never
  a re-diff of bodies — and works even when ``logging.store_bodies``
  is off (scoring happens in memory at mirror time; only the
  drilldown excerpts need stored bodies).
* **Honest scoping**: deployment mirrors only. Prompt A/B rows (also
  shadow rows) are excluded from the aggregate — their divergence is
  by design, a different prompt was tested — though they carry
  verdicts on the log surface for the prompt workflow. Rows scored
  before tier 35 (verdict NULL) are not re-guessed.
"""

from __future__ import annotations

import asyncio
import time

from gateway.config import (
    DeploymentConfig,
    GatewayConfig,
    LoggingConfig,
    ModelConfig,
)
from gateway.core.shadow_diff import (
    NEAR_THRESHOLD,
    VERDICT_DIVERGENT,
    VERDICT_EXACT,
    VERDICT_NEAR,
    VERDICT_NOT_COMPARABLE,
    VERDICT_SHADOW_ERROR,
    extract_content_text,
    score_pair,
    similarity,
)
from gateway.db import get_sessionmaker
from gateway.models import RequestLog

from .conftest import (
    ADMIN_KEY,
    _make_env,
    auth,
    chat_body,
    create_key,
    make_config,
)
from .test_alerts import _wait_for

# The mock's canned answers: buffered content for ordinary requests,
# and the streamed SSE body's assembled text ("Hel" + "lo!").
MOCK_ANSWER = "Hello from the mock!"
STREAM_ANSWER = "Hello!"
DRIFTED_ANSWER = "The candidate provider absolutely refuses to answer."


def _twin_config() -> GatewayConfig:
    """test-model with one serving deployment + one shadow twin."""
    return make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/primary", provider="openai", model="model-a"
                    ),
                    DeploymentConfig(
                        name="openai/twin", provider="openai", model="model-shadow",
                        shadow=True,
                    ),
                ],
            )
        ],
        pricing={
            "model-a": {"input": 2.0, "output": 4.0},
            "model-shadow": {"input": 2.0, "output": 4.0},
        },
    )


def _evidence_config() -> GatewayConfig:
    """test-model with one serving deployment and THREE shadow twins —
    exact (default mock content), divergent (per-model override) and
    unstable (scripted failures) — so one request fans out into three
    differently-scored pairs and the aggregate shows all the buckets."""
    return make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/primary", provider="openai", model="model-a"
                    ),
                    DeploymentConfig(
                        name="openai/twin", provider="openai",
                        model="model-shadow", shadow=True,
                    ),
                    DeploymentConfig(
                        name="openai/drifter", provider="openai",
                        model="model-b", shadow=True,
                    ),
                    DeploymentConfig(
                        name="openai/unstable", provider="openai",
                        model="model-c", shadow=True,
                    ),
                ],
            )
        ],
        pricing={
            "model-a": {"input": 2.0, "output": 4.0},
            "model-shadow": {"input": 2.0, "output": 4.0},
            "model-b": {"input": 2.0, "output": 4.0},
            "model-c": {"input": 2.0, "output": 4.0},
        },
    )


async def _groups(client, **params) -> list[dict]:
    resp = await client.get(
        "/admin/shadow-diff", params=params, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["groups"]


async def _pairs(client, **params) -> dict:
    resp = await client.get(
        "/admin/shadow-diff/pairs", params=params, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows_for(client, request_id: str) -> list[dict]:
    resp = await client.get(
        "/admin/logs", params={"request_id": request_id}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _mirror_rows(client, request_id: str) -> list[dict]:
    """The shadow rows of one request — empty until the mirror lands."""
    return [r for r in await _rows_for(client, request_id) if r["shadow"]]


async def _wait_for_result(check, timeout: float = 5.0, what: str = "condition"):
    """Like _wait_for, but returns the value the check produced (the
    suite's _wait_for returns None; these tests read the waited-for
    payload)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _chat(client, key: str = ADMIN_KEY, **body) -> str:
    resp = await client.post(
        "/v1/chat/completions", json=chat_body(**body), headers=auth(key)
    )
    assert resp.status_code == 200, resp.text
    return resp.headers["x-request-id"]


# ----------------------------------------------------------------------
# Units: extraction + bucketing
# ----------------------------------------------------------------------


def test_extract_content_text_shapes():
    # Full OpenAI-shaped response: string content.
    assert (
        extract_content_text(
            {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        )
        == "hi"
    )
    # Content-parts array (multimodal): the text members are joined.
    assert (
        extract_content_text(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "a"},
                                {"type": "text", "text": "b"},
                            ]
                        }
                    }
                ]
            }
        )
        == "ab"
    )
    # The synthesized body the pipeline logs for streamed primaries.
    assert extract_content_text({"content": "streamed", "finish_reason": "stop"}) == (
        "streamed"
    )
    # Nothing textual: tool-call replies, empty content, foreign shapes.
    assert extract_content_text(None) is None
    assert extract_content_text("string not a body") is None
    assert (
        extract_content_text(
            {"choices": [{"message": {"tool_calls": [{"id": "x"}], "content": None}}]}
        )
        is None
    )
    assert extract_content_text({"choices": [{"message": {"content": ""}}]}) is None
    assert extract_content_text({}) is None


def test_score_pair_buckets():
    # Identical text: exact, ratio 1.0.
    verdict, ratio = score_pair("same answer", "same answer", "success")
    assert verdict == VERDICT_EXACT and ratio == 1.0
    # Cosmetic drift: near, ratio in [threshold, 1.0).
    near_pair = ("Hello world", "Hello, world")
    ratio = similarity(*near_pair)
    assert NEAR_THRESHOLD <= ratio < 1.0, ratio
    assert score_pair(*near_pair, "success") == (VERDICT_NEAR, ratio)
    # Different answers: divergent, ratio < threshold.
    far_pair = (MOCK_ANSWER, DRIFTED_ANSWER)
    ratio = similarity(*far_pair)
    assert ratio < NEAR_THRESHOLD, ratio
    assert score_pair(*far_pair, "success") == (VERDICT_DIVERGENT, ratio)
    # A failed mirror is shadow_error — nothing to compare, and a
    # candidate that fails traffic is the loudest migration signal.
    assert score_pair("a", "a", "error") == (VERDICT_SHADOW_ERROR, None)
    # A missing text on either side is not_comparable — never a fake
    # 0.0 that would read as maximal divergence.
    assert score_pair(None, "a", "success") == (VERDICT_NOT_COMPARABLE, None)
    assert score_pair("a", None, "success") == (VERDICT_NOT_COMPARABLE, None)


# ----------------------------------------------------------------------
# e2e: pairs score at mirror time
# ----------------------------------------------------------------------


async def test_mirror_pair_scores_exact(tmp_path):
    async for env in _make_env(tmp_path, config=_twin_config()):
        request_id = await _chat(env.client)
        await _wait_for(
            lambda: len([r for r in env.mock.requests if r["body"].get("model") == "model-shadow"]) >= 1,
            what="mirror call",
        )
        await _wait_for(
            lambda: _mirror_rows(env.client, request_id), what="mirror row"
        )
        rows = await _rows_for(env.client, request_id)
        primary = next(r for r in rows if not r["shadow"])
        mirror = next(r for r in rows if r["shadow"])
        assert mirror["deployment"] == "openai/twin"
        assert mirror["shadow_verdict"] == VERDICT_EXACT
        assert mirror["shadow_similarity"] == 1.0
        # The client-facing row carries no verdict — only mirrors score.
        assert primary["shadow_verdict"] is None


async def test_scores_raw_provider_text_not_client_seen(tmp_path):
    """The load-bearing pin: a guardrail rewrite on the primary leg is
    NOT provider drift. The PII redactor masks the email in what the
    client sees; the mirror runs no guardrails and answers raw — yet
    the pair scores exact, because the comparison runs on the primary's
    RAW pre-phase answer, not the stored (masked) client body."""
    async for env in _make_env(tmp_path, config=_twin_config()):
        request_id = await _chat(env.client, content="PIIPLEASE")
        await _wait_for(
            lambda: _mirror_rows(env.client, request_id), what="mirror row"
        )
        rows = await _rows_for(env.client, request_id)
        mirror = next(r for r in rows if r["shadow"])
        assert mirror["shadow_verdict"] == VERDICT_EXACT
        assert mirror["shadow_similarity"] == 1.0

        # And the rewrite really happened — the two STORED bodies differ
        # (masked client-side vs raw mirror), so an exact verdict can
        # only come from raw-vs-raw scoring. (Bodies live on the DETAIL
        # endpoint; the list omits them.)
        detail = await env.client.get(
            f"/admin/logs/{mirror['id']}", headers=auth(ADMIN_KEY)
        )
        assert detail.status_code == 200, detail.text
        raw_mirror_text = extract_content_text(detail.json()["response_body"])
        assert "bot@example.com" in raw_mirror_text
        primary_id = next(r for r in rows if not r["shadow"])["id"]
        pdetail = await env.client.get(
            f"/admin/logs/{primary_id}", headers=auth(ADMIN_KEY)
        )
        client_text = extract_content_text(pdetail.json()["response_body"])
        assert client_text is not None and client_text != raw_mirror_text


async def test_streamed_primary_pairs_against_source_text(tmp_path):
    """A streamed primary pairs with its non-streaming mirror through
    the accumulated SOURCE text, captured by value at queue time (the
    old ordering could race a mirror task against the body assignment).
    The mock's stream spells 'Hello!' while its buffered answers say
    'Hello from the mock!' — the twin is scripted to the streamed text,
    so an exact verdict proves the source text (not the buffered
    default, not an unset body) was the baseline."""
    async for env in _make_env(tmp_path, config=_twin_config()):
        env.mock.model_content["model-shadow"] = STREAM_ANSWER
        request_id = await _chat(env.client, stream=True)
        await _wait_for(
            lambda: _mirror_rows(env.client, request_id), what="mirror row"
        )
        mirror = next(
            r for r in (await _rows_for(env.client, request_id)) if r["shadow"]
        )
        assert mirror["shadow_verdict"] == VERDICT_EXACT
        assert mirror["shadow_similarity"] == 1.0


async def test_divergent_pair_scored(tmp_path):
    async for env in _make_env(tmp_path, config=_twin_config()):
        env.mock.model_content["model-shadow"] = DRIFTED_ANSWER
        request_id = await _chat(env.client)
        await _wait_for(
            lambda: _mirror_rows(env.client, request_id), what="mirror row"
        )
        mirror = next(
            r for r in (await _rows_for(env.client, request_id)) if r["shadow"]
        )
        assert mirror["shadow_verdict"] == VERDICT_DIVERGENT
        assert mirror["shadow_similarity"] is not None
        assert mirror["shadow_similarity"] < NEAR_THRESHOLD


async def test_shadow_error_pair_scored(tmp_path):
    """A failing mirror call is shadow_error with no similarity — a
    candidate that cannot serve traffic is the loudest finding."""
    async for env in _make_env(tmp_path, config=_twin_config()):
        env.mock.fail_counts["model-shadow"] = 99
        request_id = await _chat(env.client)
        async def _failed_mirrors():
            return [
                r for r in await _mirror_rows(env.client, request_id)
                if r["status"] == "error"
            ]

        await _wait_for(lambda: _failed_mirrors(), what="failed mirror row")
        mirror = next(
            r
            for r in (await _rows_for(env.client, request_id))
            if r["shadow"] and r["status"] == "error"
        )
        assert mirror["shadow_verdict"] == VERDICT_SHADOW_ERROR
        assert mirror["shadow_similarity"] is None


# ----------------------------------------------------------------------
# e2e: the report + drilldown
# ----------------------------------------------------------------------


async def test_aggregate_groups_and_deltas(tmp_path):
    """One request fans out to three shadow twins; two requests make
    two pairs per twin. The aggregate must show every bucket, the
    match-rate math, the similarity mean over comparable pairs only,
    and the latency/token/cost deltas against the paired primaries."""
    async for env in _make_env(tmp_path, config=_evidence_config()):
        env.mock.model_content["model-b"] = DRIFTED_ANSWER
        env.mock.fail_counts["model-c"] = 99
        await _chat(env.client, content="first")
        await _chat(env.client, content="second")

        # wait for all three twins to have both pairs
        async def _all_pairs():
            groups = await _groups(env.client, model="test-model")
            if len(groups) == 3 and all(g["pairs"] == 2 for g in groups):
                return {g["deployment"]: g for g in groups}
            return None

        by = await _wait_for_result(
            _all_pairs, what="all three twin groups with 2 pairs"
        )

        twin = by["openai/twin"]
        assert twin["exact"] == 2 and twin["near"] == 0 and twin["divergent"] == 0
        assert twin["shadow_error"] == 0 and twin["not_comparable"] == 0
        assert twin["comparable"] == 2
        assert twin["match_rate"] == 1.0
        assert twin["similarity_mean"] == 1.0
        assert isinstance(twin["latency_delta_ms"], int)
        assert twin["tokens_delta"] == 0  # identical mock usage both legs
        assert twin["cost_delta_usd"] == 0.0  # identical pricing both legs
        assert twin["last_pair_at"] is not None

        drifter = by["openai/drifter"]
        assert drifter["exact"] == 0 and drifter["divergent"] == 2
        assert drifter["match_rate"] == 0.0
        assert drifter["similarity_mean"] is not None
        assert drifter["similarity_mean"] < NEAR_THRESHOLD

        unstable = by["openai/unstable"]
        assert unstable["shadow_error"] == 2
        assert unstable["comparable"] == 0
        # No comparable pairs: null, never a misleading number.
        assert unstable["match_rate"] is None
        assert unstable["similarity_mean"] is None
        # The failed mirror logged no cost — the delta has no baseline.
        assert unstable["cost_delta_usd"] is None

        # ?model= scopes to one model (empty for another name).
        assert (await _groups(env.client, model="test-model")) and not (
            await _groups(env.client, model="no-such-model")
        )


async def test_drilldown_pairs_and_pagination(tmp_path):
    """The divergent filter is the migration review queue: both sides'
    text side by side, plus the pair's score. limit/offset page it."""
    async for env in _make_env(tmp_path, config=_evidence_config()):
        env.mock.model_content["model-b"] = DRIFTED_ANSWER
        await _chat(env.client, content="first")
        await _chat(env.client, content="second")

        async def _divergent_total():
            data = await _pairs(
                env.client,
                model="test-model",
                deployment="openai/drifter",
                verdict="divergent",
            )
            return data if data["total"] == 2 else None

        page = await _wait_for_result(_divergent_total, what="2 divergent pairs")
        assert page["total"] == 2
        pair = page["data"][0]
        assert pair["verdict"] == "divergent"
        assert pair["similarity"] < NEAR_THRESHOLD
        assert pair["primary"]["status"] == "success"
        assert pair["shadow"]["status"] == "success"
        # Both sides' text, side by side.
        assert pair["primary"]["text"] == MOCK_ANSWER
        assert pair["shadow"]["text"] == DRIFTED_ANSWER
        assert pair["primary"]["latency_ms"] is not None

        # Pagination: one per page, two pages.
        first = await _pairs(
            env.client, model="test-model", deployment="openai/drifter",
            verdict="divergent", limit=1, offset=0,
        )
        second = await _pairs(
            env.client, model="test-model", deployment="openai/drifter",
            verdict="divergent", limit=1, offset=1,
        )
        assert first["total"] == 2 and len(first["data"]) == 1
        assert second["data"][0]["request_id"] != first["data"][0]["request_id"]


async def test_bodies_off_verdicts_survive_drilldown_lacks_text(tmp_path):
    """store_bodies=false: the score still exists (computed in memory
    at mirror time) but the drilldown honestly reports no excerpts —
    the verdict is the evidence, the bodies were never persisted."""
    config = _twin_config()
    config.logging = LoggingConfig(store_bodies=False)
    async for env in _make_env(tmp_path, config=config):
        await _chat(env.client)

        async def _group():
            groups = await _groups(env.client)
            return groups[0] if groups and groups[0]["pairs"] == 1 else None

        group = await _wait_for_result(
            _group, what="scored group despite bodies off"
        )
        assert group["exact"] == 1

        data = await _pairs(
            env.client, model="test-model", deployment="openai/twin"
        )
        assert data["total"] == 1
        pair = data["data"][0]
        assert pair["verdict"] == VERDICT_EXACT
        assert pair["primary"]["text"] is None
        assert pair["shadow"]["text"] is None


# ----------------------------------------------------------------------
# e2e: scoping + validation
# ----------------------------------------------------------------------


async def test_shadow_diff_is_platform_admin_only(tmp_path):
    """The pairs carry request bodies, which cross tenants — a tenant
    admin cannot read them (same boundary as /admin/routing), and a
    data-plane key is not an admin on this surface at all."""
    async for env in _make_env(tmp_path, config=_twin_config()):
        resp = await env.client.get("/admin/shadow-diff", params={"model": "test-model"}, headers=auth(ADMIN_KEY))
        assert resp.status_code == 200

        tenant = (
            await env.client.post(
                "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        tadmin = (
            await create_key(env.client, "tadmin", is_admin=True, tenant_id=tenant["id"])
        )["key"]
        assert (
            await env.client.get("/admin/shadow-diff", headers=auth(tadmin))
        ).status_code == 403
        assert (
            await env.client.get(
                "/admin/shadow-diff/pairs",
                params={"model": "test-model", "deployment": "openai/twin"},
                headers=auth(tadmin),
            )
        ).status_code == 403

        plain = (await create_key(env.client, "plain"))["key"]
        assert (
            await env.client.get("/admin/shadow-diff", headers=auth(plain))
        ).status_code == 403


async def test_prompt_variant_and_unscored_rows_excluded(tmp_path):
    """Deployment mirrors only. A prompt A/B row (also a shadow row,
    but its divergence is by design) and a legacy pre-tier-35 mirror
    (no verdict — never re-guessed) must not pollute the aggregate or
    the drilldown, no matter what they say."""
    async for env in _make_env(tmp_path, config=_twin_config()):
        await _chat(env.client)

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(
                RequestLog(
                    request_id="variant-1",
                    status="success",
                    model="test-model",
                    provider="openai",
                    deployment="openai/twin",
                    shadow=True,
                    prompt_variant="persona v2",
                    shadow_verdict="divergent",
                    shadow_similarity=0.1,
                    cost_usd=0.0,
                    latency_ms=3,
                    stream=False,
                )
            )
            session.add(
                RequestLog(
                    request_id="legacy-1",
                    status="success",
                    model="test-model",
                    provider="openai",
                    deployment="openai/twin",
                    shadow=True,
                    cost_usd=0.0,
                    latency_ms=3,
                    stream=False,
                )
            )
            await session.commit()

        async def _one_real_pair():
            groups = await _groups(env.client, model="test-model")
            return groups[0] if groups and groups[0]["pairs"] == 1 else None

        group = await _wait_for_result(
            _one_real_pair, what="exactly the real mirror"
        )
        assert group["exact"] == 1 and group["divergent"] == 0
        data = await _pairs(env.client, model="test-model", deployment="openai/twin")
        assert data["total"] == 1
        assert data["data"][0]["request_id"] != "variant-1"
        assert data["data"][0]["request_id"] != "legacy-1"


async def test_pairs_endpoint_validation(tmp_path):
    async for env in _make_env(tmp_path, config=_twin_config()):
        # model + deployment are required; verdict must be from the
        # vocabulary. Both answer the gateway's uniform 400 (the app
        # maps FastAPI's 422s to the OpenAI error envelope), naming
        # the offending param; unknown names answer empty, not 404.
        missing = await env.client.get(
            "/admin/shadow-diff/pairs", headers=auth(ADMIN_KEY)
        )
        assert missing.status_code == 400
        assert "model" in missing.json()["error"]["message"]
        bad = await env.client.get(
            "/admin/shadow-diff/pairs",
            params={"model": "test-model", "deployment": "openai/twin", "verdict": "bogus"},
            headers=auth(ADMIN_KEY),
        )
        assert bad.status_code == 400
        assert "verdict" in bad.json()["error"]["message"]
        empty = await env.client.get(
            "/admin/shadow-diff/pairs",
            params={"model": "test-model", "deployment": "no/such"},
            headers=auth(ADMIN_KEY),
        )
        assert empty.status_code == 200
        assert empty.json() == {
            "data": [], "total": 0, "limit": 25, "offset": 0
        }
