"""Tool-call policy (tier 30, MCP-lite): which tools a key may declare.

Model allowlists answer "which MODELS may this key use"; tool policy
answers the agent-era sibling — "which TOOLS may it declare". One
 declarative field on the key and tenant rows (fnmatch patterns over
tool names, exactly allowed_models' mechanics), one shared check at
phase 1.1 (right after model permission) that every inbound surface
funnels through, because all of them normalize tools into the OpenAI
shape before the pipeline: OpenAI chat, the Responses bridge, native
Anthropic, native Gemini.

Two layers like every ceiling: the key's allowed_tools (platform
admins bypass — they are the operators) and the tenant's pooled
allowed_tools (no bypass — the ceiling binds every member; a tool
must pass BOTH lists, so the tenant can only narrow). None = no
restriction: callers without the field anywhere are unchanged. A
request declaring a tool that matches neither list 400s
tool_not_allowed naming the offenders (the caller sent the names —
nothing to leak, everything to debug). Guardrails can overlay tools
(modified_request_fields); the policy is re-checked after the
guardrail phase so a guardrail can never inject tools past it.
"""

from __future__ import annotations

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


def _tool(name: str, description: str = "A test tool") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


async def _chat(env, key: str, tools=None, content: str = "hello", **extra):
    return await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=content, tools=tools, **extra),
        headers=auth(key),
    )


# ----------------------------------------------------------------------
# The core contract
# ----------------------------------------------------------------------


async def test_allowed_tool_passes_disallowed_rejected(tmp_path):
    async for env in _make_env(tmp_path):
        key = (
            await create_key(env.client, "tools-key", allowed_tools=["get_weather"])
        )["key"]

        ok = await _chat(env, key, tools=[_tool("get_weather")])
        assert ok.status_code == 200, ok.text
        # The tool reached the upstream request untouched.
        sent = [b for b in env.mock.bodies() if isinstance(b, dict) and "tools" in b]
        assert sent[-1]["tools"][0]["function"]["name"] == "get_weather"

        denied = await _chat(env, key, tools=[_tool("delete_database")])
        assert denied.status_code == 400
        err = denied.json()["error"]
        assert err["code"] == "tool_not_allowed"
        assert err["param"] == "tools"
        assert "delete_database" in err["message"]  # names the offender

        # Mixed: one allowed + one disallowed -> rejected, naming only
        # the offender.
        mixed = await _chat(env, key, tools=[_tool("get_weather"), _tool("steal_*")])
        # NOTE: "steal_*" is the literal tool NAME here; it does not
        # match the "get_weather" pattern.
        assert mixed.status_code == 400
        assert "steal_*" in mixed.json()["error"]["message"]
        assert "get_weather" not in mixed.json()["error"]["message"]


async def test_fnmatch_patterns(tmp_path):
    async for env in _make_env(tmp_path):
        key = (
            await create_key(env.client, "prefix-key", allowed_tools=["search_*", "read_*"])
        )["key"]
        assert (
            await _chat(env, key, tools=[_tool("search_web")])
        ).status_code == 200
        assert (
            await _chat(env, key, tools=[_tool("search_docs"), _tool("read_file")])
        ).status_code == 200
        assert (
            await _chat(env, key, tools=[_tool("write_file")])
        ).status_code == 400


async def test_no_tools_and_unrestricted_callers_unchanged(tmp_path):
    async for env in _make_env(tmp_path):
        # No tools in the request: passes trivially, no policy to apply.
        free = (await create_key(env.client, "free"))["key"]
        assert (await _chat(env, free)).status_code == 200

        # No allowed_tools anywhere: any tool passes through untouched.
        assert (
            await _chat(env, free, tools=[_tool("anything_at_all")])
        ).status_code == 200

        # Admin/env key: exempt, like every key-layer ceiling.
        assert (
            await _chat(env, ADMIN_KEY, tools=[_tool("anything_at_all")])
        ).status_code == 200

        # A restricted key with NO tools is also fine.
        restricted = (
            await create_key(env.client, "restricted", allowed_tools=["x"])
        )["key"]
        assert (await _chat(env, restricted)).status_code == 200


# ----------------------------------------------------------------------
# Every inbound surface funnels through the shared check
# ----------------------------------------------------------------------


async def test_every_surface_enforces_the_policy(tmp_path):
    async for env in _make_env(tmp_path):
        key = (
            await create_key(env.client, "surface-key", allowed_tools=["good_tool"])
        )["key"]
        bad = [_tool("bad_tool")]

        # OpenAI chat
        assert (await _chat(env, key, tools=bad)).status_code == 400

        # Responses bridge: function tools in the OpenAI Responses shape;
        # it answers with the standard OpenAI error envelope.
        responses = await env.client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": "hello",
                "tools": [
                    {"type": "function", "name": "bad_tool", "parameters": {}}
                ],
            },
            headers=auth(key),
        )
        assert responses.status_code == 400
        assert responses.json()["error"]["code"] == "tool_not_allowed"

        # Native Anthropic: Anthropic's own error envelope (the message
        # still names the offender; the envelope has type, not code).
        anthropic = await env.client.post(
            "/v1/messages",
            json={
                "model": "test-model",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"name": "bad_tool", "description": "d", "input_schema": {}}],
            },
            headers={"x-api-key": key},
        )
        assert anthropic.status_code == 400
        body = anthropic.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
        assert "bad_tool" in body["error"]["message"]

        # Native Gemini: Google's RPC error envelope (code is the HTTP
        # status there).
        gemini = await env.client.post(
            "/v1/models/test-model:generateContent",
            json={
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                "tools": [
                    {"functionDeclarations": [{"name": "bad_tool", "description": "d"}]}
                ],
            },
            headers={"x-api-key": key},
        )
        assert gemini.status_code == 400
        gbody = gemini.json()
        assert gbody["error"]["code"] == 400
        assert "bad_tool" in gbody["error"]["message"]

        # And the allowed twin passes on every surface.
        good = [_tool("good_tool")]
        assert (await _chat(env, key, tools=good)).status_code == 200


# ----------------------------------------------------------------------
# Two layers: key ∩ tenant, the tenant can only narrow
# ----------------------------------------------------------------------


async def test_tenant_policy_binds_every_member(tmp_path):
    async for env in _make_env(tmp_path):
        tenant = (
            await env.client.post(
                "/admin/tenants",
                json={"name": "acme", "allowed_tools": ["safe_*"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()

        # Member with NO key-level policy: the tenant's binds it.
        member = (
            await create_key(env.client, "member", tenant_id=tenant["id"])
        )["key"]
        assert (
            await _chat(env, member, tools=[_tool("safe_search")])
        ).status_code == 200
        assert (
            await _chat(env, member, tools=[_tool("unsafe_search")])
        ).status_code == 400

        # Member key WIDER than the tenant: intersection — both must
        # allow. The tenant can only narrow.
        wide = (
            await create_key(
                env.client, "wide", tenant_id=tenant["id"], allowed_tools=["*_search"]
            )
        )["key"]
        assert (
            await _chat(env, wide, tools=[_tool("unsafe_search")])
        ).status_code == 400  # key allows, tenant doesn't

        # Member key NARROWER than the tenant: passes its own stricter
        # self first.
        narrow = (
            await create_key(
                env.client, "narrow", tenant_id=tenant["id"], allowed_tools=["safe_web"]
            )
        )["key"]
        assert (
            await _chat(env, narrow, tools=[_tool("safe_web")])
        ).status_code == 200
        assert (
            await _chat(env, narrow, tools=[_tool("safe_search")])
        ).status_code == 400  # tenant allows, key doesn't

        # Tenant ADMINS are not exempt from the tenant ceiling (same
        # as models — an admin manages keys, not the policy).
        tadmin = (
            await create_key(
                env.client, "tadmin", tenant_id=tenant["id"], is_admin=True
            )
        )["key"]
        assert (
            await _chat(env, tadmin, tools=[_tool("unsafe_search")])
        ).status_code == 400


# ----------------------------------------------------------------------
# Guardrails can modify tools — the policy is re-checked after them
# ----------------------------------------------------------------------


async def test_guardrail_cannot_inject_tools_past_policy(tmp_path):
    # A custom guardrail overlays tools via modified_request_fields.
    # The tool policy is a hard boundary: re-checked at 4.6, the request
    # 400s instead of leaking the injected tool upstream.
    from gateway.guardrails.base import Guardrail, GuardrailResult
    from gateway.schemas import Tool

    class ToolInjector(Guardrail):
        name = "tool_injector"

        async def check_request(self, request, ctx):
            import copy

            tools = copy.deepcopy(request.tools or [])
            # A real Tool object — the manager's model_copy(update=...)
            # does not re-validate, and the policy check reads
            # tool.function.name off the model.
            tools.append(Tool.model_validate(_tool("injected_evil_tool")))
            return GuardrailResult.modified(
                fields={"tools": tools}, reason="injected a tool"
            )

    async for env in _make_env(tmp_path):
        key = (
            await create_key(env.client, "strict", allowed_tools=["good_tool"])
        )["key"]
        env.app.state.pipeline.guardrails.register(ToolInjector())

        resp = await _chat(env, key, tools=[_tool("good_tool")])
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert err["code"] == "tool_not_allowed"
        assert "injected_evil_tool" in err["message"]
        # The injected tool never reached the upstream.
        sent = [b for b in env.mock.bodies() if isinstance(b, dict) and "tools" in b]
        assert all(
            t["function"]["name"] != "injected_evil_tool" for b in sent for t in b["tools"]
        )


# ----------------------------------------------------------------------
# Admin API surface: create/patch/roundtrip
# ----------------------------------------------------------------------


async def test_admin_api_roundtrip(tmp_path):
    async for env in _make_env(tmp_path):
        created = await create_key(
            env.client, "tools-key", allowed_tools=["Search_*", " get_weather "]
        )
        # Patterns are stored as given (fnmatch is case-sensitive on the
        # wire; the admin UI lowercases nothing here — the operator's
        # patterns are theirs, same contract as allowed_models).
        assert created["allowed_tools"] == ["Search_*", " get_weather "]

        patched = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"allowed_tools": ["only_this"]},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["allowed_tools"] == ["only_this"]

        cleared = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"allowed_tools": None},
            headers=auth(ADMIN_KEY),
        )
        assert cleared.status_code == 200
        assert cleared.json()["allowed_tools"] is None

        tenant = (
            await env.client.post(
                "/admin/tenants",
                json={"name": "acme", "allowed_tools": ["t_tool"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        assert tenant["allowed_tools"] == ["t_tool"]
        tpatched = await env.client.patch(
            f"/admin/tenants/{tenant['id']}",
            json={"allowed_tools": None},
            headers=auth(ADMIN_KEY),
        )
        assert tpatched.json()["allowed_tools"] is None
