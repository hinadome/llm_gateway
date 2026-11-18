"""Capture-replay harness: diff real traffic against the current config.

The contract, three legs:

* **Capture** — ``GET /admin/logs/export`` writes the replayable
  request log (stored bodies, shadow mirrors excluded) as JSONL with
  the recorded baseline: caller identity (key_id — the exact-caller
  column — plus key_name), the inbound route, model, status,
  deployment and guardrail verdicts.
* **Replay** — ``POST /admin/replay`` re-executes each captured
  request through the request-policy phases ONLY (resolution, prompt
  policy, permissions, guardrails, routing selection) as the recorded
  caller's *current* key — no upstream calls, no counters, no cache,
  no spend, no log rows, no lifecycle events, and guardrail counters
  suppressed (ctx.dry_run) so a harness run can never trip block
  alerts.
* **Diff** — each line compares the prediction against the recorded
  baseline: status, model, request-phase guardrail verdicts,
  deployment membership. The changed lines are what the current
  config would change about that traffic — the safety net for a hot
  reload: capture, apply the candidate config, replay, read the diff,
  keep or roll back.

Rows recorded as ``error`` are exported but not comparable (their
outcome was upstream/counter state a dry run cannot re-run). Entries
with no virtual-key identity (bare requests, admin-key captures)
replay as the calling admin's context. Tenant admins replay only
their own tenant's traffic — the same boundary as every other key
operation.
"""

from __future__ import annotations

import json

import httpx

from gateway.config import (
    DeploymentConfig,
    GuardrailConfig,
    GuardrailScope,
    LoggingConfig,
    ModelConfig,
    ReplayConfig,
)
from gateway.observability.metrics import GUARDRAIL_BLOCKS, REPLAY
from gateway.schemas import ChatCompletionRequest

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_native_anthropic import anthropic_body


def _blocks_total() -> float:
    """Sum of gateway_guardrail_blocks_total across all label sets."""
    return sum(
        sample.value
        for metric in GUARDRAIL_BLOCKS.collect()
        for sample in metric.samples
        if sample.name == "gateway_guardrail_blocks_total"
    )


async def _capture(client: httpx.AsyncClient, **params) -> list[dict]:
    """The exported capture, parsed into entry dicts (newest first)."""
    resp = await client.get(
        "/admin/logs/export", params=params or None, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in resp.text.splitlines() if line]


async def _replay(client: httpx.AsyncClient, entries: list, key: str = ADMIN_KEY) -> dict:
    resp = await client.post("/admin/replay", json={"entries": entries}, headers=auth(key))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _log_total(client: httpx.AsyncClient) -> int:
    return (await client.get("/admin/logs", headers=auth(ADMIN_KEY))).json()["total"]


def _upstream_chat_calls(env) -> int:
    return len([r for r in env.mock.requests if "messages" in r["body"]])


# -- capture ------------------------------------------------------------------


async def test_export_returns_replayable_capture(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "captor"))["key"]
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello gateway"),
            headers=auth(key),
        )
        env.mock.fail_counts["model-c"] = 3  # other-model: single deployment
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model", content="boom"),
            headers=auth(key),
        )
        await env.client.post(  # native surface -> its route is recorded
            "/v1/messages",
            json=anthropic_body("native hello"),
            headers={"x-api-key": key},
        )

        rows = await _capture(env.client)
        assert len(rows) == 3  # newest first: native, error, success
        assert {r["route"] for r in rows} == {"chat", "messages"}

        native = next(r for r in rows if r["route"] == "messages")
        assert native["status"] == "success"
        assert native["key_name"] == "captor"
        assert native["key_id"]  # the exact-caller column
        assert native["deployment"] == "openai/primary"
        assert native["request_body"]["messages"][0]["content"] == "native hello"
        assert native["comparable"] is True
        assert native["skip_reason"] is None

        error_row = next(r for r in rows if r["status"] == "error")
        assert error_row["comparable"] is False
        assert "cannot re-run" in error_row["skip_reason"]

        # Filters narrow the capture like /admin/logs.
        only_success = await _capture(env.client, status="success")
        assert {r["status"] for r in only_success} == {"success"}
        only_model = await _capture(env.client, model="other-model")
        assert [r["model"] for r in only_model] == ["other-model"]
        assert len(await _capture(env.client, limit=1)) == 1


async def test_export_excludes_shadow_mirrors_and_bodiless_rows(tmp_path):
    shadow_models = [
        ModelConfig(
            name="test-model",
            deployments=[
                DeploymentConfig(
                    name="openai/primary", provider="openai", model="model-a"
                ),
                DeploymentConfig(
                    name="openai/shadow",
                    provider="openai",
                    model="model-b",
                    shadow=True,
                ),
            ],
        ),
    ]
    async for env in _make_env(tmp_path, config=make_config(models=shadow_models)):
        key = (await create_key(env.client, "shadowed"))["key"]
        await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        await env.app.state.pipeline.drain_background_tasks()
        # Both the primary and the mirrored shadow row exist in the log...
        assert (await _log_total(env.client)) == 2
        # ...but a capture is client traffic only.
        rows = await _capture(env.client)
        assert len(rows) == 1
        assert rows[0]["deployment"] == "openai/primary"


async def test_export_is_empty_without_stored_bodies(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(logging=LoggingConfig(store_bodies=False))
    ):
        key = (await create_key(env.client, "bodiless"))["key"]
        await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        assert (await _log_total(env.client)) == 1
        # Without stored bodies there is nothing to replay: the capture
        # is empty, not an error.
        assert await _capture(env.client) == []


# -- replay + diff ------------------------------------------------------------


async def test_replay_same_config_reports_unchanged(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "steady"))["key"]
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello gateway"),
            headers=auth(key),
        )
        entries = await _capture(env.client)
        assert len(entries) == 1

        upstream_before = _upstream_chat_calls(env)
        logs_before = await _log_total(env.client)
        blocks_before = _blocks_total()

        report = await _replay(env.client, entries)
        assert report["replayed"] == 1
        assert report["unchanged"] == 1
        assert report["changed"] == 0
        line = report["results"][0]
        assert line["result"] == "unchanged"
        assert line["changes"] is None
        assert line["predicted"]["status"] == "success"
        assert line["predicted"]["model"] == "test-model"
        assert line["predicted"]["deployments"] == ["openai/primary", "openai/fallback"]
        assert line["recorded"]["status"] == "success"

        # A dry run touches nothing: no upstream calls, no log rows,
        # no guardrail enforcement counters.
        assert _upstream_chat_calls(env) == upstream_before
        assert await _log_total(env.client) == logs_before
        assert _blocks_total() == blocks_before


async def test_replay_flags_newly_blocked_traffic(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "chatty"))["key"]
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello gateway"),
            headers=auth(key),
        )
        entries = await _capture(env.client)
        assert entries[0]["status"] == "success"

        # Candidate config: a blocklist that now catches this content.
        candidate = make_config()
        candidate.guardrails.append(
            GuardrailConfig(
                name="keyword_blocklist",
                enabled=True,
                params={"blocked_terms": ["hello gateway"]},
            )
        )
        env.app.state.pipeline.apply_config(candidate)

        blocks_before = _blocks_total()
        report = await _replay(env.client, entries)
        line = report["results"][0]
        assert line["result"] == "changed"
        assert set(line["changes"]) == {"status", "guardrail_actions"}
        assert line["predicted"]["status"] == "blocked"
        assert "keyword_blocklist" in line["predicted"]["error"]["message"]
        verdicts = [
            (a["guardrail"], a["action"]) for a in line["predicted"]["guardrail_actions"]
        ]
        assert ("keyword_blocklist", "block") in verdicts

        # The prediction never touched the enforcement counter — a
        # harness run cannot trip block alerts.
        assert _blocks_total() == blocks_before


async def test_replay_flags_removed_model(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "mover"))["key"]
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model", content="Hello"),
            headers=auth(key),
        )
        entries = await _capture(env.client)
        assert entries[0]["model"] == "other-model"

        # Candidate config: the model is gone entirely.
        candidate = make_config(
            models=[m for m in make_config().models if m.name != "other-model"]
        )
        env.app.state.pipeline.apply_config(candidate)

        report = await _replay(env.client, entries)
        line = report["results"][0]
        assert line["result"] == "changed"
        assert "status" in line["changes"]
        assert line["predicted"]["status"] == "error"
        assert line["predicted"]["error"]["code"] == "model_not_found"


async def test_replay_flags_removed_deployment(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "routed"))["key"]
        await env.client.post(
            "/v1/chat/completions", json=chat_body(content="Hello"), headers=auth(key)
        )
        entries = await _capture(env.client)
        assert entries[0]["deployment"] == "openai/primary"

        # Candidate config: the serving deployment is gone; only the
        # fallback remains.
        candidate = make_config(
            models=[
                ModelConfig(
                    name="test-model",
                    deployments=[
                        DeploymentConfig(
                            name="openai/fallback", provider="openai", model="model-b"
                        )
                    ],
                ),
                ModelConfig(
                    name="other-model",
                    deployments=[
                        DeploymentConfig(
                            name="openai/other", provider="openai", model="model-c"
                        )
                    ],
                ),
            ]
        )
        env.app.state.pipeline.apply_config(candidate)

        report = await _replay(env.client, entries)
        line = report["results"][0]
        assert line["result"] == "changed"
        assert line["changes"] == ["deployment"]
        assert line["predicted"]["deployments"] == ["openai/fallback"]


async def test_replay_deleted_key_predicts_rejection(tmp_path):
    async for env in _make_env(tmp_path):
        created = await create_key(env.client, "gone-later")
        key = created["key"]
        await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        entries = await _capture(env.client)

        # The key is revoked after the capture — its traffic would now
        # 401, which the replay must surface as a change.
        revoked = await env.client.delete(
            f"/admin/keys/{created['id']}", headers=auth(ADMIN_KEY)
        )
        assert revoked.status_code == 200

        report = await _replay(env.client, entries)
        line = report["results"][0]
        assert line["result"] == "changed"
        assert line["changes"] == ["status"]
        assert line["predicted"]["error"]["code"] == "replay_caller_rejected"
        # DELETE /admin/keys soft-revokes (is_active=false).
        assert "revoked" in line["predicted"]["error"]["message"]


async def test_replay_error_rows_not_comparable_admin_traffic_replays_as_caller(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "mixed"))["key"]
        env.mock.fail_counts["model-c"] = 3
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model", content="boom"),
            headers=auth(key),
        )
        # Admin-key traffic: no key policy to be faithful to, so it
        # replays as the calling admin's context (permission checks
        # bypass — such lines can only diff model/guardrail/routing).
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="admin hello"),
            headers=auth(ADMIN_KEY),
        )
        entries = await _capture(env.client)
        assert len(entries) == 2

        report = await _replay(env.client, entries)
        upstream_error = next(
            r for r in report["results"] if r["skip_reason"] and "state" in r["skip_reason"]
        )
        # The upstream-error line still predicts (policy passed — the
        # failure was transient state), just without a diff.
        assert upstream_error["predicted"]["status"] == "success"
        assert "state, not policy" in upstream_error["skip_reason"]
        # The admin-key line diffs normally: unchanged.
        admin_line = next(r for r in report["results"] if r["recorded"] is not None)
        assert admin_line["result"] == "unchanged"
        assert report["not_comparable"] == 1
        assert report["unchanged"] == 1


async def test_replay_blind_request_prediction_without_diff(tmp_path):
    async for env in _make_env(tmp_path):
        report = await _replay(
            env.client, [{"request_body": chat_body(content="a fresh request")}]
        )
        line = report["results"][0]
        # No recorded baseline: a prediction, not a diff.
        assert line["result"] == "not_comparable"
        assert line["recorded"] is None
        assert "no comparable recorded baseline" in line["skip_reason"]
        assert line["predicted"]["status"] == "success"
        assert line["predicted"]["deployments"] == ["openai/primary", "openai/fallback"]
        # Nothing was executed upstream for the blind request either.
        assert _upstream_chat_calls(env) == 0


async def test_replay_route_fidelity_respects_guardrail_scoping(tmp_path):
    # A blocklist scoped to the OpenAI chat route only. The native
    # capture must stay unchanged (its route excludes the guardrail)
    # while the chat capture stays blocked — the replay honors the
    # captured route, or route-scoped policy would diff wrongly.
    config = make_config()
    config.guardrails.append(
        GuardrailConfig(
            name="keyword_blocklist",
            enabled=True,
            scope=GuardrailScope(routes=["chat"]),
            params={"blocked_terms": ["hello gateway"]},
        )
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "scoped"))["key"]
        await env.client.post(  # chat: the blocklist applies live
            "/v1/chat/completions",
            json=chat_body(content="Hello gateway"),
            headers=auth(key),
        )
        await env.client.post(  # native: out of scope live
            "/v1/messages",
            json=anthropic_body("Hello gateway"),
            headers={"x-api-key": key},
        )
        entries = {e["route"]: e for e in await _capture(env.client)}
        assert entries["chat"]["status"] == "blocked"
        assert entries["messages"]["status"] == "success"

        report = await _replay(env.client, list(entries.values()))
        results = report["results"]
        blocked_line = next(r for r in results if r["recorded"]["status"] == "blocked")
        native_line = next(r for r in results if r["recorded"]["status"] == "success")
        assert blocked_line["result"] == "unchanged"  # still blocked
        assert native_line["result"] == "unchanged"  # still out of scope


async def test_replay_tenant_admin_boundary(tmp_path):
    async for env in _make_env(tmp_path):
        tenant_a = (
            await env.client.post(
                "/admin/tenants", json={"name": "alpha"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        tenant_b = (
            await env.client.post(
                "/admin/tenants", json={"name": "beta"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        key_a = (
            await create_key(env.client, "alpha-member", tenant_id=tenant_a["id"])
        )["key"]
        admin_b = await create_key(
            env.client, "beta-admin", is_admin=True, tenant_id=tenant_b["id"]
        )
        await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key_a)
        )
        entries = await _capture(env.client)
        assert len(entries) == 1

        # Beta's tenant admin may replay — but alpha's capture is not
        # theirs: same boundary as every other key operation.
        report = await _replay(env.client, entries, key=admin_b["key"])
        line = report["results"][0]
        assert line["result"] == "not_comparable"
        assert line["skip_reason"] == "entry belongs to another tenant"

        # The platform admin replays anything.
        platform = await _replay(env.client, entries)
        assert platform["results"][0]["result"] == "unchanged"


async def test_replay_config_gates(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(replay=ReplayConfig(enabled=False))
    ):
        resp = await env.client.post(
            "/admin/replay",
            json={"entries": [{"request_body": chat_body()}]},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "replay_disabled"

    async for env in _make_env(
        tmp_path, config=make_config(replay=ReplayConfig(max_entries=2))
    ):
        resp = await env.client.post(
            "/admin/replay",
            json={"entries": [{"request_body": chat_body()}] * 3},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "too_many_replay_entries"


async def test_replay_reports_prompt_policy_and_metrics(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "prompted"))["key"]
        await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        entries = await _capture(env.client)
        assert len(entries) == 1

        before = {
            k: REPLAY.labels(k)._value.get()
            for k in ("changed", "unchanged", "not_comparable", "error")
        }
        report = await _replay(env.client, entries)
        # The recorded request carries no system message: no managed
        # prompt applies.
        assert report["results"][0]["predicted"]["prompt_policy_applied"] is False

        # Candidate config: a managed system prompt now prepends.
        candidate = make_config()
        candidate.prompts.system_prompt = "You are a pirate."
        env.app.state.pipeline.apply_config(candidate)
        report = await _replay(env.client, entries)
        line = report["results"][0]
        assert line["predicted"]["prompt_policy_applied"] is True
        # Prompt policy is reported, not diffed (the record predates it).
        assert line["result"] == "unchanged"

        # The harness counter saw the runs (unchanged lines).
        assert REPLAY.labels("unchanged")._value.get() >= before["unchanged"] + 2


# -- unit level -----------------------------------------------------------------


async def test_diff_ignores_response_phase_verdicts():
    from gateway.core.replay import _diff, _request_verdicts

    recorded = {
        "model": "m",
        "status": "success",
        "deployment": "d1",
        "guardrail_actions": [
            {"guardrail": "pii_redaction", "phase": "request", "action": "modify"},
            {"guardrail": "pii_redaction", "phase": "response", "action": "modify"},
            {"guardrail": "output_moderation", "phase": "response", "action": "allow"},
        ],
    }
    predicted_same_request_phase = {
        "model": "m",
        "status": "success",
        "deployments": ["d1", "d2"],
        "guardrail_actions": [
            {"guardrail": "pii_redaction", "phase": "request", "action": "modify"}
        ],
    }
    # Response-phase verdicts are invisible to the diff (the replay
    # stops at the request phase).
    assert _request_verdicts(recorded["guardrail_actions"]) == [
        ("pii_redaction", "modify")
    ]
    assert _diff(recorded, predicted_same_request_phase) == []

    predicted_blocked = {
        "model": "m",
        "status": "blocked",
        "deployments": None,
        "guardrail_actions": [
            {"guardrail": "x", "phase": "request", "action": "block"}
        ],
    }
    assert _diff(recorded, predicted_blocked) == ["status", "guardrail_actions"]

    predicted_rerouted = {
        "model": "m",
        "status": "success",
        "deployments": ["d9"],
        "guardrail_actions": [],
    }
    assert _diff(recorded, predicted_rerouted) == ["guardrail_actions", "deployment"]


async def test_non_chat_rows_fail_chat_validation():
    from pydantic import ValidationError

    try:
        ChatCompletionRequest.model_validate({"input": ["embedding-shaped"]})
        raised = False
    except ValidationError:
        raised = True
    assert raised  # non-chat rows take the not_comparable path
