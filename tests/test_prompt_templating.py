"""Tests for prompt templating (``{{variable}}`` substitution).

Placeholders in message text — and in the gateway-managed system prompt
— resolve against a variables map: the global ``prompts.variables``
merged with the calling key's ``prompt_variables`` (per-key winning,
the established override pattern). Only *known* names are replaced;
unknown placeholders and literal braces (code snippets, JSON examples)
pass through untouched, so enabling templating never corrupts prompts
that didn't ask for it. No variables configured anywhere → the whole
step is a no-op.
"""

from __future__ import annotations

from gateway.config import PromptsConfig
from gateway.core.prompts import apply_prompt_policy, substitute
from gateway.schemas import ChatCompletionRequest

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


def _request(**body) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(chat_body(**body))


# ----------------------------------------------------------------------
# substitute (unit)
# ----------------------------------------------------------------------


def test_substitutes_known_names():
    assert substitute("Work for {{company}}.", {"company": "Acme"}) == "Work for Acme."


def test_inner_whitespace_is_tolerated():
    assert substitute("Hey {{ name }}!", {"name": "Ada"}) == "Hey Ada!"


def test_unknown_placeholders_untouched():
    text = "Use {{unknown_var}} and {{  also_unknown }} literally"
    assert substitute(text, {"company": "Acme"}) == text


def test_literal_braces_are_safe():
    assert substitute('return {"a": 1}', {"a": "x"}) == 'return {"a": 1}'
    assert substitute("no placeholders at all", {"a": "x"}) == "no placeholders at all"


def test_no_variables_is_identity():
    assert substitute("keep {{this}}", None) == "keep {{this}}"
    assert substitute("keep {{this}}", {}) == "keep {{this}}"


def test_non_string_values_coerce():
    assert substitute("port {{port}}", {"port": 8080}) == "port 8080"


def test_repeated_placeholders():
    assert substitute("{{x}} and {{x}}", {"x": "1"}) == "1 and 1"


# ----------------------------------------------------------------------
# apply_prompt_policy with variables (unit)
# ----------------------------------------------------------------------


def test_variables_resolve_in_message_content():
    request = _request(content="Work for {{company}}")
    result, changed = apply_prompt_policy(request, None, {"company": "Acme"})
    assert changed
    assert result.messages[-1].content == "Work for Acme"


def test_variables_resolve_in_text_parts_only():
    request = _request(content="Work for {{company}}")
    request.messages[0].content = [
        {"type": "text", "text": "Work for {{company}}"},
        {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
    ]
    result, changed = apply_prompt_policy(request, None, {"company": "Acme"})
    assert changed
    parts = result.messages[0].content
    assert parts[0] == {"type": "text", "text": "Work for Acme"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "https://x/img.png"}}


def test_variables_resolve_in_injected_system_prompt():
    request = _request(content="hi")
    result, changed = apply_prompt_policy(
        request, "You work for {{company}}.", {"company": "Acme"}
    )
    assert changed
    assert result.messages[0].content == "You work for Acme."
    assert result.messages[0].role == "system"


def test_client_system_gets_variables_without_injection():
    """The client's own system message is never replaced — but its
    placeholders still resolve (it's message text like any other)."""
    from gateway.schemas import Message

    request = _request(content="hi")
    request.messages.insert(0, Message(role="system", content="Tone: {{tone}}"))
    result, changed = apply_prompt_policy(request, "managed", {"tone": "formal"})
    assert changed
    assert result.messages[0].content == "Tone: formal"  # still the client's message
    assert result.messages[0].role == "system"


def test_untouched_request_returns_same_object():
    request = _request(content="no placeholders")
    result, changed = apply_prompt_policy(request, None, {"company": "Acme"})
    assert not changed
    assert result is request


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------


async def test_global_variables_reach_provider(tmp_path):
    config = make_config(
        prompts=PromptsConfig(variables={"company": "Acme", "tone": "formal"})
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "templated"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Write about {{company}} in a {{tone}} style"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        upstream = env.mock.requests[-1]["body"]
        assert upstream["messages"][-1]["content"] == "Write about Acme in a formal style"


async def test_key_variables_override_global(tmp_path):
    config = make_config(prompts=PromptsConfig(variables={"company": "Acme"}))
    async for env in _make_env(tmp_path, config=config):
        key = (
            await create_key(
                env.client, "own-vars", prompt_variables={"company": "Globex", "tone": "casual"}
            )
        )["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="{{company}} / {{tone}}"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert env.mock.requests[-1]["body"]["messages"][-1]["content"] == "Globex / casual"


async def test_system_prompt_and_variables_compose(tmp_path):
    """The managed system prompt is itself templated against the merged
    variables — one place to define the persona per team."""
    async for env in _make_env(tmp_path):
        key = (
            await create_key(
                env.client,
                "persona",
                system_prompt="You are the assistant of {{company}}. Tone: {{tone}}.",
                prompt_variables={"company": "Acme", "tone": "formal"},
            )
        )["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        assert resp.status_code == 200
        upstream = env.mock.requests[-1]["body"]
        assert upstream["messages"][0] == {
            "role": "system",
            "content": "You are the assistant of Acme. Tone: formal.",
        }


async def test_unknown_placeholders_reach_provider_verbatim(tmp_path):
    config = make_config(prompts=PromptsConfig(variables={"company": "Acme"}))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "literal"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content='Return JSON like {"a": 1}; use {{not_a_variable}}'),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert (
            env.mock.requests[-1]["body"]["messages"][-1]["content"]
            == 'Return JSON like {"a": 1}; use {{not_a_variable}}'
        )


async def test_native_formats_and_bridge_are_templated(tmp_path):
    """Substitution lives in the pipeline, so every inbound format gets
    it: Anthropic blocks, Gemini parts and Responses input alike."""
    async for env in _make_env(tmp_path):
        key = (
            await create_key(env.client, "native-vars", prompt_variables={"company": "Acme"})
        )["key"]

        anthropic = await env.client.post(
            "/v1/messages",
            json={
                "model": "test-model",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "For {{company}}"}]}],
            },
            headers={"x-api-key": key},
        )
        assert anthropic.status_code == 200
        assert env.mock.requests[-1]["body"]["messages"][-1]["content"] == "For Acme"

        gemini = await env.client.post(
            "/v1/models/test-model:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "For {{company}}"}]}]},
            headers={"x-goog-api-key": key},
        )
        assert gemini.status_code == 200
        assert env.mock.requests[-1]["body"]["messages"][-1]["content"] == "For Acme"

        responses = await env.client.post(
            "/v1/responses",
            json={"model": "test-model", "input": "For {{company}}"},
            headers=auth(key),
        )
        assert responses.status_code == 200
        assert env.mock.requests[-1]["body"]["messages"][-1]["content"] == "For Acme"


async def test_resolved_values_differentiate_cache_entries(tmp_path):
    """Same template, different variables (per key) → different effective
    messages → separate cache entries."""
    async for env in _make_env(tmp_path):
        acme = (
            await create_key(env.client, "acme", prompt_variables={"company": "Acme"})
        )["key"]
        globex = (
            await create_key(env.client, "globex", prompt_variables={"company": "Globex"})
        )["key"]

        for _ in range(2):  # second call hits the exact cache
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="Summarize for {{company}}"),
                headers=auth(acme),
            )
            assert resp.status_code == 200
        assert len(env.mock.requests) == 1

        other = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Summarize for {{company}}"),
            headers=auth(globex),
        )
        assert other.status_code == 200
        assert len(env.mock.requests) == 2


async def test_admin_api_roundtrip(tmp_path):
    async for env in _make_env(tmp_path):
        created = await create_key(
            env.client, "patched-vars", prompt_variables={"company": "Acme"}
        )
        assert created["prompt_variables"] == {"company": "Acme"}

        patched = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"prompt_variables": {"company": "Globex", "extra": "1"}},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["prompt_variables"] == {
            "company": "Globex",
            "extra": "1",
        }

        # takes effect without a restart
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="For {{company}}"),
            headers=auth(created["key"]),
        )
        assert resp.status_code == 200
        assert env.mock.requests[-1]["body"]["messages"][-1]["content"] == "For Globex"

        cleared = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"prompt_variables": None},
            headers=auth(ADMIN_KEY),
        )
        assert cleared.status_code == 200
        assert cleared.json()["prompt_variables"] is None
