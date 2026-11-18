"""Admin playground — tier 41: the dashboard's own chat client.

The contract pinned here, in one paragraph: POST /admin/playground/chat
(platform-admin-only) runs ONE chat completion through the REAL
pipeline — same auth-context snapshot, same guardrails, budgets,
residency, rate limits, routing, cache and billing a live request
from that key would get — and answers with the client-facing
completion VERBATIM beside the diagnostics of what happened while
producing it (the request-log row the run just wrote: request id,
key, model/deployment/provider, status, tokens, cost, latency,
cache-hit, guardrail verdicts). ``key`` names a virtual key to
impersonate, resolved through the same chain the JWT auth step uses
(name -> active/expiry -> tenant -> ``auth_context_from_key``) with
``auth_method="playground"`` on every row and event; omitted, the
calling platform admin's own context runs the chat. Errors pass
through verbatim — the exact envelope a real client would receive —
while every attempt (success, blocked or failed) is audited as
``playground.chat`` with the calling admin as the actor and the
impersonated key, model, request id and outcome in the details.
Non-streaming only: ``stream: true`` is an explicit 400.

The playground does not write configuration: every test here runs on
the default env fixture, never touching the repo's ./config.yaml.
"""

from __future__ import annotations

import httpx

from gateway.config import AuditConfig
from gateway.schemas import PlaygroundChatRequest

from .conftest import ADMIN_KEY, auth, create_key
from .test_audit_webhooks import AuditSink, _make_audit_env, _wait_for
from .test_trials import _drain


def _body(
    content: str = "Hello",
    model: str = "test-model",
    key: str | None = None,
    **extra,
) -> dict:
    body: dict = {"model": model, "messages": [{"role": "user", "content": content}]}
    if key is not None:
        body["key"] = key
    body.update(extra)
    return body


async def _playground(client, body: dict) -> httpx.Response:
    return await client.post(
        "/admin/playground/chat", json=body, headers=auth(ADMIN_KEY)
    )


# ----------------------------------------------------------------------
# Units: the request schema
# ----------------------------------------------------------------------


def test_playground_request_schema():
    """The body is closed and honest: key optional (None = the calling
    admin), the two chat knobs default to the pipeline's own, and
    stream is a DECLARED field defaulting false — parsing true is fine
    so the refusal is the endpoint's explicit, explained 400, never a
    silent ignore."""
    req = PlaygroundChatRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert req.key is None
    assert req.stream is False
    assert req.temperature is None and req.max_tokens is None
    streaming = PlaygroundChatRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    )
    assert streaming.stream is True  # the endpoint rejects it, not pydantic


# ----------------------------------------------------------------------
# e2e: one request, one answer, one diagnostics panel
# ----------------------------------------------------------------------


async def test_playground_happy_path_diagnostics_pinned(env):
    """The admin's own chat (no key named): the response is the exact
    client-facing OpenAI completion a /v1 caller would receive, and
    the diagnostics are the request-log row the run just wrote — every
    field pinned, including the playground relabel that makes the row
    say where the request was issued."""
    resp = await _playground(env.client, _body())
    assert resp.status_code == 200, resp.text
    data = resp.json()

    completion = data["response"]
    assert completion["model"] == "test-model"
    content = completion["choices"][0]["message"]["content"]
    assert content == "Hello from the mock!"  # the mock's content-based reply
    assert completion["usage"]["total_tokens"] > 0

    d = data["diagnostics"]
    assert d["request_id"] == resp.headers["x-request-id"]
    assert d["key"] == "admin"
    assert d["auth_method"] == "playground"  # the row says where it came from
    assert d["key_id"] is None  # admin traffic bills no virtual key
    assert d["model"] == "test-model"
    assert d["deployment"] == "openai/primary"
    assert d["provider"] == "openai"
    assert d["route"] == "chat"  # guardrail route-scopes behave identically
    assert d["status"] == "success"
    assert d["error_message"] is None
    assert d["prompt_tokens"] > 0 and d["completion_tokens"] > 0
    assert d["cost_usd"] > 0
    assert d["latency_ms"] >= 0
    assert d["ttft_ms"] is None  # non-streaming
    assert d["cache_hit"] is False
    assert d["guardrail_actions"] == []
    assert d["logged"] is True


async def test_playground_impersonated_key_is_billed_and_logged(env):
    """The point of naming a key: its policy reality binds and its
    billing accrues — spend lands on the key row, the log row carries
    the key's name and id, and auth_method='playground' is the one
    thing separating it from the key's live traffic."""
    created = await create_key(env.client, "imp")
    key_id = created["id"]
    assert created["spend_usd"] == 0

    resp = await _playground(env.client, _body(key="imp", content="bill me"))
    assert resp.status_code == 200, resp.text
    d = resp.json()["diagnostics"]
    assert d["key"] == "imp"
    assert d["key_id"] == key_id
    assert d["auth_method"] == "playground"
    assert d["status"] == "success"

    # Billed identically to live traffic. Spend accounting is a
    # background task — drain it before reading the key row (the
    # documented pattern for anything that waits on background work).
    await _drain(env)
    after = (
        await env.client.get(f"/admin/keys/{key_id}", headers=auth(ADMIN_KEY))
    ).json()
    assert after["spend_usd"] > 0
    # And the row sits in the normal logs, indistinguishable except
    # for auth_method.
    logs = (
        await env.client.get(
            "/admin/logs", params={"limit": 50}, headers=auth(ADMIN_KEY)
        )
    ).json()["data"]
    row = next(r for r in logs if r["request_id"] == d["request_id"])
    assert row["key_name"] == "imp" and row["auth_method"] == "playground"


async def test_playground_guardrail_verdicts_visible(env):
    """Guardrails run on playground traffic like on any other — a PII
    response is masked (the client sees [REDACTED:EMAIL]) and the
    diagnostics show the guardrail's modify verdict."""
    await create_key(env.client, "scoped")
    resp = await _playground(
        env.client,
        _body(key="scoped", content="my email is jane@example.com PIIPLEASE"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    content = data["response"]["choices"][0]["message"]["content"]
    assert "bot@example.com" not in content
    assert "[REDACTED:EMAIL]" in content
    d = data["diagnostics"]
    assert d["status"] == "success"  # masked, not blocked
    assert any(
        a["guardrail"] == "pii_redaction" and a["action"] == "modify"
        for a in d["guardrail_actions"]
    )


async def test_playground_blocked_completion_verbatim(env):
    """A response-phase block is the client's truth: the completion
    that ships is the content_filter body a real client would receive
    (200, finish_reason content_filter, no content) — and the
    diagnostics say what the logs will say: status 'blocked' with the
    output_moderation block verdict."""
    resp = await _playground(env.client, _body(content="BADOUTPUT please"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    choice = data["response"]["choices"][0]
    assert choice["finish_reason"] == "content_filter"
    assert choice["message"]["content"] is None
    d = data["diagnostics"]
    assert d["status"] == "blocked"
    assert any(
        a["guardrail"] == "output_moderation" and a["action"] == "block"
        for a in d["guardrail_actions"]
    )


async def test_playground_cache_hit_on_repeat(env):
    """The playground rides the real response cache: an identical
    repeat request answers from it, and the diagnostics say so."""
    body = _body(content="say exactly this once")
    first = await _playground(env.client, body)
    assert first.status_code == 200
    assert first.json()["diagnostics"]["cache_hit"] is False

    second = await _playground(env.client, body)
    assert second.status_code == 200
    d = second.json()["diagnostics"]
    assert d["cache_hit"] is True
    assert d["status"] == "success"
    # The completion still shipped: the cached client-facing body.
    assert (
        second.json()["response"]["choices"][0]["message"]["content"]
        == first.json()["response"]["choices"][0]["message"]["content"]
    )


async def test_playground_rate_limits_apply_verbatim(env):
    """The impersonated key's rate limits bind exactly as live traffic:
    rpm_limit=1 admits the first request and refuses the second with
    the same 429 a real client would see."""
    key = (await create_key(env.client, "throttled", rpm_limit=1))["key"]

    first = await _playground(env.client, _body(key="throttled", content="one"))
    assert first.status_code == 200, first.text

    second = await _playground(env.client, _body(key="throttled", content="two"))
    assert second.status_code == 429
    assert "rate" in second.json()["error"]["message"].lower()


async def test_playground_errors_pass_through_verbatim(env):
    """An upstream failure surfaces as the real client's envelope —
    unchanged, unwrapped: the error the /v1 caller would get is the
    error the operator sees. Both upstream models fail (the default
    env has a priority-200 fallback — a real failure must exhaust the
    whole ladder, rescue included)."""
    env.mock.fail_counts["model-a"] = 999
    env.mock.fail_counts["model-b"] = 999
    resp = await _playground(env.client, _body(content="boom"))
    assert resp.status_code >= 400
    assert "error" in resp.json()
    assert resp.json()["error"]["message"]

    # The same request through /v1 produces the same envelope shape.
    twin = await env.client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "boom"}]},
        headers=auth(ADMIN_KEY),
    )
    assert twin.status_code == resp.status_code
    assert twin.json()["error"]["type"] == resp.json()["error"]["type"]


async def test_playground_unknown_and_dead_keys(env):
    """Key resolution mirrors the JWT step honestly: unknown name 404s
    (the admin-UI vocabulary, not the raw-key 401 — the caller named a
    key, they get told about the key); inactive and expired keys are
    refused with the reason, never silently run as someone else."""
    missing = await _playground(env.client, _body(key="no-such-key"))
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "invalid_request_error"

    dead = await create_key(env.client, "dead")
    await env.client.patch(
        f"/admin/keys/{dead['id']}",
        json={"is_active": False},
        headers=auth(ADMIN_KEY),
    )
    inactive = await _playground(env.client, _body(key="dead"))
    assert inactive.status_code == 400
    assert inactive.json()["error"]["code"] == "key_inactive"

    from datetime import datetime, timedelta, timezone

    expired_at = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    old = await create_key(env.client, "old", expires_at=expired_at)
    expired = await _playground(env.client, _body(key="old"))
    assert expired.status_code == 400
    assert "expired" in expired.json()["error"]["message"]


async def test_playground_stream_rejected(env):
    """Non-streaming only, and the refusal is an explained 400 — the
    playground is one request, one answer, one diagnostics panel."""
    resp = await _playground(env.client, _body(stream=True))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "stream_not_supported"


async def test_playground_permissions_and_validation(env):
    """Platform-admin-only (tenant admins and plain keys are 403 —
    impersonating any key on the platform is the operator's power),
    and the body validates like any admin surface: model and messages
    required, an unknown model passes through to the router's own
    404 verbatim."""
    tenant = (
        await env.client.post(
            "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
        )
    ).json()
    tadmin = (
        await create_key(env.client, "tadmin", is_admin=True, tenant_id=tenant["id"])
    )["key"]
    plain = (await create_key(env.client, "plain"))["key"]
    for key in (tadmin, plain):
        resp = await env.client.post(
            "/admin/playground/chat",
            json=_body(),
            headers=auth(key),
        )
        assert resp.status_code == 403, resp.text

    # Validation: the two required fields are enforced by the schema,
    # surfacing as the gateway's standard 400 envelope.
    no_model = await env.client.post(
        "/admin/playground/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=auth(ADMIN_KEY),
    )
    assert no_model.status_code == 400
    assert no_model.json()["error"]["type"] == "invalid_request_error"
    assert "model" in no_model.json()["error"]["message"]
    no_messages = await env.client.post(
        "/admin/playground/chat",
        json={"model": "test-model"},
        headers=auth(ADMIN_KEY),
    )
    assert no_messages.status_code == 400
    assert "messages" in no_messages.json()["error"]["message"]

    # An unknown model is the router's own error, passed through.
    unknown = await _playground(env.client, _body(model="no-such-model"))
    assert unknown.status_code == 404
    assert "no-such-model" in unknown.json()["error"]["message"]


# ----------------------------------------------------------------------
# e2e: the audit trail — the price of the grant
# ----------------------------------------------------------------------


async def test_playground_every_attempt_audited(tmp_path):
    """An admin impersonating tenant keys is a sensitive probe, and
    the trail is the price of the grant: every attempt — success,
    blocked or failed — lands a playground.chat audit record with the
    calling ADMIN as the actor and the impersonated key, model,
    request id and outcome in the details."""
    sink = AuditSink()
    async for env in _make_audit_env(
        tmp_path,
        audit=AuditConfig(webhook_url="https://audit.test/hook"),
        sink=sink,
    ):
        await create_key(env.client, "audited")
        resp = await _playground(
            env.client, _body(key="audited", content="watch me")
        )
        assert resp.status_code == 200
        request_id = resp.json()["diagnostics"]["request_id"]

        # A failed attempt: the upstream refuses (both models — the
        # fallback must fail too, or the ladder rescues the request).
        env.mock.fail_counts["model-a"] = 999
        env.mock.fail_counts["model-b"] = 999
        failed = await _playground(
            env.client, _body(key="audited", content="boom")
        )
        assert failed.status_code >= 400

        await _wait_for(
            lambda: len(
                [p for p in sink.payloads if p["event"] == "playground.chat"]
            )
            >= 2
        )
        records = [p for p in sink.payloads if p["event"] == "playground.chat"]
        success = next(r for r in records if r["details"]["request_id"] == request_id)
        assert success["actor"]["name"] == "admin"  # the CALLING admin, not the key
        assert success["actor"]["auth_method"] != "playground"
        assert success["details"]["key"] == "audited"
        assert success["details"]["model"] == "test-model"
        assert success["details"]["outcome"] == "success"
        assert success["details"]["cost_usd"] > 0

        error = next(
            r for r in records if r["details"]["outcome"] == "error"
        )
        assert error["details"]["key"] == "audited"
        assert error["details"]["error"]

        # And no-key runs are audited too: key None, actor admin. (The
        # mock recovers first — this run is about the plain admin path,
        # not another failure.)
        env.mock.fail_counts.clear()
        ok = await _playground(env.client, _body(content="no key"))
        assert ok.status_code == 200
        await _wait_for(
            lambda: any(
                p["event"] == "playground.chat"
                and p["details"].get("request_id")
                == ok.json()["diagnostics"]["request_id"]
                for p in sink.payloads
            )
        )
        bare = next(
            p
            for p in sink.payloads
            if p["event"] == "playground.chat"
            and p["details"].get("request_id") == ok.json()["diagnostics"]["request_id"]
        )
        assert bare["details"]["key"] is None
        break
