"""Tests for the wider guardrail surface (Tier 8).

Three features on the guardrail framework:

* **Wider request-phase surface** — a request-phase guardrail can modify
  the model (reject-and-redirect: "too expensive for the flagship model,
  serve it on the small one instead of blocking") and arbitrary request
  fields (``temperature``, ``tools``, ...), not just messages.
  Modifications chain: each guardrail sees the previous one's output.
  The pipeline re-checks the key's model allowlist after a redirect
  (fail closed — redirect must never bypass permissions) and routes,
  logs, meters and caches against the final model.
* **Mid-stream rewriting** — guardrails can *rewrite* streamed output as
  it streams (``rewrite_stream_text``), instead of only blocking after
  flagged content already reached the client. Rewriters hold back text
  that might still change and flush before the finish chunk, so nothing
  they want masked ever leaks, no matter how the provider chunked it.
  ``pii_redaction`` gains ``mask_in_stream`` implementing the reserve
  heuristic.
* **Per-route scoping** — ``scope.routes`` / ``exclude_routes`` with
  fnmatch patterns over the inbound API surface ("chat", "responses",
  "messages", "gemini"): different policy per inbound format.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.config import GuardrailConfig, GuardrailScope
from gateway.core.context import AuthContext, RequestContext
from gateway.guardrails.base import Guardrail, GuardrailResult
from gateway.guardrails.builtins.pii_redaction import PIIRedactionGuardrail
from gateway.guardrails.manager import GuardrailManager
from gateway.schemas import ChatCompletionRequest

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _wait_for


# ----------------------------------------------------------------------
# Test guardrails
# ----------------------------------------------------------------------


class _Redirector(Guardrail):
    """Redirects every request it sees to `target` (from params)."""

    name = "test_redirector"
    description = "redirects everything to a target model"

    def __init__(self, params=None):
        super().__init__(params)
        self.target = (params or {}).get("target", "other-model")

    async def check_request(self, request, ctx):
        return GuardrailResult.redirect(self.target, "policy: cheaper model for this content")


class _Kitchen(Guardrail):
    """One MODIFY verdict carrying messages + model + fields at once."""

    name = "test_kitchen_sink"

    async def check_request(self, request, ctx):
        messages = [
            *request.messages,
            request.messages[-1].model_copy(update={"content": "redirected"}),
        ]
        return GuardrailResult.modified(
            messages=messages,
            model="other-model",
            fields={"temperature": 0.5},
            reason="all three",
        )


class _FieldRewriter(Guardrail):
    name = "test_field_rewriter"
    description = "rewrites sampling params"

    async def check_request(self, request, ctx):
        return GuardrailResult.modified(
            fields={"temperature": 0.123}, reason="clamp temperature"
        )


class _MessageAppender(Guardrail):
    """Appends one marker message — used to prove modifications chain."""

    def __init__(self, params=None):
        super().__init__(params)
        self.marker = (params or {}).get("marker", "marker")
        self.name = (params or {}).get("name", "test_appender")

    async def check_request(self, request, ctx):
        messages = [
            *request.messages,
            request.messages[-1].model_copy(update={"content": f"{self.marker}"}),
        ]
        return GuardrailResult.modified(messages=messages, reason=f"append {self.marker}")


class _Replace(Guardrail):
    """Stream rewriter: replaces one substring with another."""

    supports_stream_rewrite = True

    def __init__(self, params=None, name="test_replacer", src="foo", dst="bar"):
        super().__init__(params)
        self.name = name
        self.src = src
        self.dst = dst

    async def rewrite_stream_text(self, text: str, *, final: bool = False):
        return text.replace(self.src, self.dst), ""


class _Recorder(Guardrail):
    """Records what check_stream_delta saw (to prove block checks see the
    SOURCE text, not the rewrite)."""

    name = "test_recorder"
    description = "records stream observations"

    def __init__(self, params=None):
        super().__init__(params)
        self.seen: list[str] = []

    async def check_stream_delta(self, request, accumulated_text, delta_text, ctx):
        self.seen.append(accumulated_text)
        return None


class _Blocker(Guardrail):
    name = "test_blocker"
    description = "blocks every request it sees"

    async def check_request(self, request, ctx):
        return GuardrailResult.block("blocked by test_blocker")


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )


def _ctx(key_name: str = "k", model: str = "m", route: str | None = None) -> RequestContext:
    return RequestContext(
        request_id="r",
        auth=AuthContext(key_name=key_name),
        model=model,
        route=route,
    )


def _redirect_config(target: str = "other-model", **extra):
    return make_config(
        guardrails=[
            GuardrailConfig(
                name="test_redirector",
                class_path="tests.test_guardrail_surface:_Redirector",
                params={"target": target},
                **extra,
            )
        ]
    )


# ----------------------------------------------------------------------
# F1: request-phase surface — redirect + fields + chaining
# ----------------------------------------------------------------------


async def _log_rows(client, **params) -> list:
    resp = await client.get("/admin/logs", params=params, headers=auth(ADMIN_KEY))
    assert resp.status_code == 200
    return resp.json()["data"]


async def test_redirect_changes_model_end_to_end(tmp_path):
    """A guardrail redirect reroutes the request: the upstream body, the
    echoed response model and the log row all see the target model."""
    async for env in _make_env(tmp_path, config=_redirect_config()):
        key = (await create_key(env.client, "redirect-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["model"] == "other-model"  # redirected public name
        # Upstream served the target model's deployment (native model-c).
        assert env.mock.requests[0]["body"]["model"] == "model-c"

        async def _redirected_row() -> bool:
            rows = await _log_rows(env.client, key_name="redirect-user")
            return any(r["model"] == "other-model" for r in rows)

        await _wait_for(_redirected_row, what="redirected log row")
        break


async def test_redirect_rechecks_allowlist(tmp_path):
    """Redirect must never bypass the key's model allowlist — the
    redirected-to model is permission-checked again, fail closed
    (presented as the standard model_not_allowed/404 error)."""
    async for env in _make_env(tmp_path, config=_redirect_config()):
        key = (
            await create_key(env.client, "locked", allowed_models=["test-model"])
        )["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 404
        error = resp.json()["error"]
        assert error["code"] == "model_not_found"
        assert "other-model" in error["message"]
        # Nothing reached the upstream.
        assert env.mock.requests == []


async def test_redirect_admin_bypasses_allowlist(tmp_path):
    """Admins bypass the allowlist at step 1 — and after a redirect too."""
    async for env in _make_env(tmp_path, config=_redirect_config()):
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "other-model"


async def test_redirect_unknown_target_404s(tmp_path):
    """A redirect to a name that is not a configured model fails in the
    router with a clear 404 — the redirect is where the request goes."""
    async for env in _make_env(tmp_path, config=_redirect_config(target="ghost-model")):
        key = (await create_key(env.client, "any"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 404
        assert "ghost-model" in resp.json()["error"]["message"]


async def test_modified_request_fields_applied(tmp_path):
    """Guardrails can rewrite sampling params, not just messages."""
    async for env in _make_env(
        tmp_path,
        config=make_config(
            guardrails=[
                GuardrailConfig(
                    name="test_field_rewriter",
                    class_path="tests.test_guardrail_surface:_FieldRewriter",
                )
            ]
        ),
    ):
        key = (await create_key(env.client, "param-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(temperature=1.0),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert env.mock.requests[0]["body"]["temperature"] == 0.123


async def test_redirect_and_messages_compose(tmp_path):
    """One MODIFY can carry messages + model + fields at once."""
    async for env in _make_env(
        tmp_path,
        config=make_config(
            guardrails=[
                GuardrailConfig(
                    name="test_kitchen_sink",
                    class_path="tests.test_guardrail_surface:_Kitchen",
                )
            ]
        ),
    ):
        key = (await create_key(env.client, "sink-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        body = env.mock.requests[0]["body"]
        assert body["model"] == "model-c"
        assert body["temperature"] == 0.5
        assert body["messages"][-1]["content"] == "redirected"


async def test_modifications_chain_across_guardrails(tmp_path):
    """Each guardrail sees the previous one's output: two appending
    guardrails both apply (the original code silently dropped the first)."""
    async for env in _make_env(
        tmp_path,
        config=make_config(
            guardrails=[
                GuardrailConfig(
                    name="test_appender_a",
                    class_path="tests.test_guardrail_surface:_MessageAppender",
                    params={"marker": "first-marker", "name": "test_appender_a"},
                ),
                GuardrailConfig(
                    name="test_appender_b",
                    class_path="tests.test_guardrail_surface:_MessageAppender",
                    params={"marker": "second-marker", "name": "test_appender_b"},
                ),
            ]
        ),
    ):
        key = (await create_key(env.client, "chain-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        contents = [m["content"] for m in env.mock.requests[0]["body"]["messages"]]
        assert contents[-1] == "second-marker"
        assert contents[-2] == "first-marker"  # both modifications survived


async def test_observe_mode_never_redirects(tmp_path):
    """Observe mode suppresses redirect verdicts like any other."""
    async for env in _make_env(
        tmp_path, config=_redirect_config(mode="observe")
    ):
        key = (await create_key(env.client, "observer"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "test-model"  # untouched
        assert env.mock.requests[0]["body"]["model"] == "model-a"


async def test_redirect_flows_into_cache_identity(tmp_path):
    """The redirected model is part of cache identity: an identical request
    that is NOT redirected cannot be served the redirect's cached answer."""
    config = make_config(
        guardrails=[
            GuardrailConfig(
                name="test_redirector",
                class_path="tests.test_guardrail_surface:_Redirector",
                scope=GuardrailScope(keys=["redirected-*"]),
            )
        ]
    )
    async for env in _make_env(tmp_path, config=config):
        redirected = (
            await create_key(env.client, "redirected-user")
        )["key"]
        plain = (await create_key(env.client, "plain-user"))["key"]

        first = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="same"), headers=auth(redirected)
        )
        assert first.status_code == 200
        assert first.json()["model"] == "other-model"

        second = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="same"), headers=auth(plain)
        )
        assert second.status_code == 200
        # Served fresh on the original model — not the redirect's cache row.
        assert second.json()["model"] == "test-model"
        assert second.headers["x-gateway-cache"] == "MISS"
        assert env.mock.requests[1]["body"]["model"] == "model-a"


# ----------------------------------------------------------------------
# F1: request_limits on_exceed: redirect
# ----------------------------------------------------------------------


async def test_request_limits_redirect_mode(tmp_path):
    """Oversized requests are redirected to the cheaper model instead of
    blocked — the built-in reject-and-redirect policy."""
    config = make_config(
        guardrails=[
            GuardrailConfig(
                name="request_limits",
                enabled=True,
                params={
                    "max_messages": 2,
                    "on_exceed": "redirect",
                    "redirect_model": "other-model",
                },
            )
        ]
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "limit-user"))["key"]

        oversized = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(
                messages=[
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                    {"role": "user", "content": "three"},
                ]
            ),
            headers=auth(key),
        )
        assert oversized.status_code == 200, oversized.text
        assert oversized.json()["model"] == "other-model"
        assert env.mock.requests[0]["body"]["model"] == "model-c"

        # Within limits: served on the requested model, untouched.
        fine = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="just one"), headers=auth(key)
        )
        assert fine.status_code == 200
        assert fine.json()["model"] == "test-model"


def test_request_limits_redirect_requires_target():
    """on_exceed: redirect without redirect_model is a config error at
    load time, not a silent fall-back to blocking."""
    with pytest.raises(ValueError, match="redirect_model"):
        GuardrailManager.from_config(
            [
                GuardrailConfig(
                    name="request_limits",
                    enabled=True,
                    params={"max_messages": 2, "on_exceed": "redirect"},
                )
            ]
        )


# ----------------------------------------------------------------------
# F2: mid-stream rewriting — units
# ----------------------------------------------------------------------


async def test_pii_stream_rewriter_holds_boundary_email():
    """An email split across provider chunks never leaks: every partial
    form stays pending until it resolves, and the final flush redacts."""
    guardrail = PIIRedactionGuardrail({"mask_in_stream": True})
    assert guardrail.supports_stream_rewrite is True

    emitted = ""
    pending = ""
    for piece in ["You can reach bob@exa", "mple.com about", " the invoice"]:
        emit, pending = await guardrail.rewrite_stream_text(pending + piece)
        emitted += emit
        # No prefix of the email ever leaks in a partial form.
        assert "bob@e" not in emitted

    tail, final_pending = await guardrail.rewrite_stream_text(pending, final=True)
    emitted += tail
    assert final_pending == ""
    assert "bob@example.com" not in emitted
    assert "[REDACTED:EMAIL]" in emitted


async def test_pii_stream_final_redacts_all():
    guardrail = PIIRedactionGuardrail(
        {"mask_in_stream": True, "stream_reserve_chars": 10}
    )
    text = "call 555-123-4567 or mail a@b.com"
    emitted, pending = await guardrail.rewrite_stream_text(text, final=True)
    assert pending == ""
    assert "555-123-4567" not in emitted
    assert "a@b.com" not in emitted
    assert "[REDACTED:PHONE]" in emitted
    assert "[REDACTED:EMAIL]" in emitted


async def test_pii_stream_reserve_flushes_before_final():
    """Text older than the reserve is emitted as it streams — only the
    tail (and possible incomplete matches) waits for the final flush."""
    guardrail = PIIRedactionGuardrail(
        {"mask_in_stream": True, "stream_reserve_chars": 10}
    )
    emitted = ""
    pending = ""
    # 20 safe chars, then an email, then enough tail to push the safe
    # text out of the reserve zone.
    for piece in ["x" * 20, " bob@example.com ", "y" * 25]:
        emit, pending = await guardrail.rewrite_stream_text(pending + piece)
        emitted += emit
    # The safe prefix streamed out before any final flush happened, and
    # the email was masked on its way through (not held to the end).
    assert emitted.startswith("x" * 20)
    assert "[REDACTED:EMAIL]" in emitted
    assert "bob@example.com" not in emitted
    # Only the held-back tail remains for the final flush (no PII left).
    tail, pending = await guardrail.rewrite_stream_text(pending, final=True)
    assert pending == ""
    assert tail == "y" * 10


async def test_pii_stream_match_crossing_boundary_held():
    """A match that ends past the reserve boundary pulls the emit boundary
    back to its start — a match is never emitted half-redacted."""
    guardrail = PIIRedactionGuardrail(
        {"mask_in_stream": True, "stream_reserve_chars": 10}
    )
    # 20 safe chars + 12-char phone + " tail" = 37; safe boundary at 27.
    text = "x" * 20 + "555-123-4567 tail"
    emitted, pending = await guardrail.rewrite_stream_text(text)
    # Everything from the phone's start onward is held back...
    assert pending == text[20:]
    # ...and the emitted prefix is fully redacted-safe (no PII in it).
    assert "555" not in emitted


async def test_stream_rewriters_chain():
    """Each rewriter sees the previous one's output."""
    manager = GuardrailManager()
    manager.register(_Replace(name="first", src="foo", dst="bar"))
    manager.register(_Replace(name="second", src="bar", dst="baz"))
    state = manager.new_stream_rewrite_state(_request(), _ctx())
    assert state is not None
    out = await manager.run_stream_rewrite(_request(), _ctx(), state, "say foo now")
    assert out == "say baz now"  # foo -> bar (first) -> baz (second)


async def test_stream_rewrite_observe_discards_output():
    """Observe mode runs the rewriter (would-be modifications are counted
    and reported) but the client sees the unrewritten text."""
    manager = GuardrailManager()
    observing = _Replace(name="watcher", src="secret", dst="[X]")
    observing.mode = "observe"
    manager.register(observing)
    state = manager.new_stream_rewrite_state(_request(), _ctx())
    assert state is not None

    out = await manager.run_stream_rewrite(
        _request(), _ctx(), state, "my secret text"
    )
    assert out == "my secret text"  # untouched
    assert state.modified_counts["watcher"] == 1

    ctx = _ctx()
    manager.record_stream_rewrite(ctx, state)
    (entry,) = ctx.guardrail_actions
    assert entry["action"] == "modify"
    assert entry["mode"] == "observe"
    assert entry["phase"] == "stream"


async def test_new_stream_rewrite_state_none_without_rewriters():
    """No rewriting guardrails -> no state, zero stream overhead."""
    manager = GuardrailManager()
    manager.register(_Blocker())
    assert manager.new_stream_rewrite_state(_request(), _ctx()) is None


# ----------------------------------------------------------------------
# F2: mid-stream rewriting — e2e
# ----------------------------------------------------------------------


def _mock_stream(*deltas: str) -> str:
    """SSE body with the given content deltas (mirrors the mock's chunk
    shapes: role delta, content deltas, finish chunk, usage, DONE)."""
    lines = [
        'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
    ]
    for d in deltas:
        lines.append(
            'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
            '"created":1700000000,"model":"model-a","choices":[{"index":0,'
            '"delta":{"content":' + json.dumps(d) + '},"finish_reason":null}]}\n\n'
        )
    lines.append(
        'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    lines.append(
        'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    )
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _stream_config(**pii_params) -> "make_config":
    params = {"mask_in_stream": True}
    params.update(pii_params)
    return make_config(
        guardrails=[GuardrailConfig(name="pii_redaction", enabled=True, params=params)]
    )


async def test_streamed_pii_masked_end_to_end(tmp_path):
    """PII in a streamed response is masked *as it streams*: the client
    never receives the raw email, no matter how it was chunked."""
    async for env in _make_env(tmp_path, config=_stream_config()):
        env.mock.stream_text = _mock_stream(
            "Contact alice@exa", "mple.com for", " the details."
        )
        key = (await create_key(env.client, "stream-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stream me", stream=True),
            headers=auth(key),
        )
        assert resp.status_code == 200
        body = (await resp.aread()).decode()
        # The raw PII never reached the client in any chunk.
        assert "alice@example.com" not in body
        assert "alice@exa" not in body
        # The masked form did (flushed before the finish chunk).
        assert "[REDACTED:EMAIL]" in body
        # Stream structure intact: finish chunk + DONE still delivered.
        assert '"finish_reason": "stop"' in body or '"finish_reason":"stop"' in body
        assert "data: [DONE]" in body
        # The finish chunk is the LAST content-bearing SSE event before DONE.
        assert body.rstrip().endswith("data: [DONE]")


async def test_stream_rewrite_log_and_cache(tmp_path):
    """After a rewritten stream: one stream-modify guardrail_actions entry
    on the log row, the stored response body has the masked text, and a
    subsequent identical (non-streamed) request is served that masked
    text from the cache."""
    async for env in _make_env(tmp_path, config=_stream_config()):
        env.mock.stream_text = _mock_stream("mail me at bob@exa", "mple.com ok")
        key = (await create_key(env.client, "cache-user"))["key"]

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="ask", stream=True),
            headers=auth(key),
        )
        assert resp.status_code == 200
        await resp.aread()

        async def _stream_row():
            rows = await _log_rows(env.client, key_name="cache-user")
            return rows[0] if rows and rows[0]["guardrail_actions"] else None

        row = None

        async def _wait_row():
            nonlocal row
            row = await _stream_row()
            return row is not None

        await _wait_for(_wait_row, what="stream log row")
        stream_actions = row["guardrail_actions"]
        assert any(
            a["guardrail"] == "pii_redaction"
            and a["phase"] == "stream"
            and a["action"] == "modify"
            for a in stream_actions
        )

        # The identical non-streamed request hits the cache and gets the
        # masked content (the cache stores what the client saw).
        second = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="ask"), headers=auth(key)
        )
        assert second.status_code == 200
        assert second.headers["x-gateway-cache"] == "HIT"
        content = second.json()["choices"][0]["message"]["content"]
        assert "bob@example.com" not in content
        assert "[REDACTED:EMAIL]" in content
        assert len(env.mock.requests) == 1  # served from cache


async def test_stream_block_checks_see_source_text(tmp_path):
    """check_stream_delta sees the SOURCE text from the provider, even
    while a rewriter changes what the client receives."""
    recorder = _Recorder()

    async for env in _make_env(tmp_path, config=_stream_config()):
        # Register the recorder on the live manager (scope-free, runs on
        # every stream chunk alongside the rewriter).
        env.app.state.pipeline.guardrails.register(recorder)
        env.mock.stream_text = _mock_stream("secret agent at c@d.com", " done")
        key = (await create_key(env.client, "source-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="ask", stream=True),
            headers=auth(key),
        )
        assert resp.status_code == 200
        raw = (await resp.aread()).decode()
        # The client saw the masked text...
        assert "[REDACTED:EMAIL]" in raw
        assert "c@d.com" not in raw
        # ...but the block-style checks saw the raw source text.
        assert any("c@d.com" in seen for seen in recorder.seen)
        break


# ----------------------------------------------------------------------
# F3: per-route scoping
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,route,expect_run",
    [
        (None, "chat", True),
        (GuardrailScope(routes=["chat"]), "chat", True),
        (GuardrailScope(routes=["chat"]), "messages", False),
        (GuardrailScope(routes=["chat", "responses"]), "responses", True),
        (GuardrailScope(routes=["res*"]), "responses", True),
        (GuardrailScope(routes=["res*"]), "gemini", False),
        # route-less (programmatic pipeline calls) never matches routes
        (GuardrailScope(routes=["*"]), None, False),
        # ... and has no route to exclude either
        (GuardrailScope(exclude_routes=["messages"]), None, True),
        (GuardrailScope(exclude_routes=["messages"]), "messages", False),
        (GuardrailScope(exclude_routes=["messages", "gemini"]), "chat", True),
        (GuardrailScope(exclude_routes=["messages", "gemini"]), "gemini", False),
        # routes compose with keys (both must match)
        (GuardrailScope(keys=["t*"], routes=["chat"]), "chat", True),
        (GuardrailScope(keys=["t*"], routes=["chat"]), "messages", False),
    ],
)
def test_route_scope_matrix(scope, route, expect_run):
    manager = GuardrailManager.from_config(
        [
            GuardrailConfig(
                name="test_blocker",
                class_path="tests.test_guardrail_surface:_Blocker",
                scope=scope,
            )
        ]
    )
    active = manager._active(_ctx(key_name="team-a", route=route))
    assert (len(active) == 1) is expect_run


async def test_route_scoped_guardrail_only_blocks_its_route(tmp_path):
    """A guardrail scoped to routes: ["chat"] blocks /v1/chat/completions
    but leaves the other three inbound formats untouched."""
    config = make_config(
        guardrails=[
            GuardrailConfig(
                name="test_blocker",
                class_path="tests.test_guardrail_surface:_Blocker",
                scope=GuardrailScope(routes=["chat"]),
            )
        ]
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "route-user"))["key"]

        chat = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert chat.status_code == 400
        assert chat.json()["error"]["code"] == "content_policy_violation"

        anthropic = await env.client.post(
            "/v1/messages",
            json={
                "model": "test-model",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"x-api-key": key},
        )
        assert anthropic.status_code == 200

        responses = await env.client.post(
            "/v1/responses",
            json={"model": "test-model", "input": "hello"},
            headers=auth(key),
        )
        assert responses.status_code == 200

        gemini = await env.client.post(
            "/v1/models/test-model:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            headers={"x-goog-api-key": key},
        )
        assert gemini.status_code == 200
        break


async def test_route_exclude_scoping(tmp_path):
    """exclude_routes: ["messages"] — the native Anthropic format is
    exempt from a guardrail that blocks everywhere else."""
    config = make_config(
        guardrails=[
            GuardrailConfig(
                name="test_blocker",
                class_path="tests.test_guardrail_surface:_Blocker",
                scope=GuardrailScope(exclude_routes=["messages"]),
            )
        ]
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "excl-user"))["key"]

        chat = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert chat.status_code == 400

        anthropic = await env.client.post(
            "/v1/messages",
            json={
                "model": "test-model",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"x-api-key": key},
        )
        assert anthropic.status_code == 200
        break
