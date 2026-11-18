"""Context-window management: fit requests to the model's window pre-flight.

The contract: each public model may declare ``context_window`` (total
tokens, input + output); the ``context_window`` guardrail enforces it
at the request phase — estimated input (~4 chars/token, the same
heuristic as TPM, including the managed system prompt) plus the
response budget (``max_tokens`` or ``reserve_output_tokens``) must
fit. Over-window requests 400 with OpenAI-style
context_length_exceeded wording *before* any upstream call — or, with
``on_exceed: redirect``, are served by a bigger model instead. Models
without a declared window are unmanaged (or governed by
``default_window``). The window is model metadata surfaced at
/admin/routing and re-resolved when another guardrail redirects.
"""

from __future__ import annotations

import pytest

from gateway.config import (
    CacheConfig,
    DeploymentConfig,
    GuardrailConfig,
    ModelConfig,
    PromptsConfig,
)
from gateway.guardrails.builtins.context_window import ContextWindowGuardrail

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


def _models():
    return [
        ModelConfig(
            name="small-model",
            context_window=1000,
            deployments=[
                DeploymentConfig(name="openai/small", provider="openai", model="model-a")
            ],
        ),
        ModelConfig(
            name="big-model",
            context_window=100_000,
            deployments=[
                DeploymentConfig(name="openai/big", provider="openai", model="model-b")
            ],
        ),
        ModelConfig(
            name="unmanaged-model",
            deployments=[
                DeploymentConfig(name="openai/unmanaged", provider="openai", model="model-c")
            ],
        ),
    ]


def _config(**params):
    return make_config(
        models=_models(),
        cache=CacheConfig(enabled=False),
        guardrails=[
            GuardrailConfig(name="context_window", enabled=True, params=params),
            # A second, later guardrail proves the window verdict runs in
            # the normal request phase (composes with the chain).
            GuardrailConfig(name="prompt_injection", enabled=True, params={"threshold": 3}),
        ],
    )


# "Hello" estimates at 1 token — max_tokens drives the arithmetic.
OVER = chat_body(model="small-model", content="Hello", max_tokens=5000)
FITS = chat_body(model="small-model", content="Hello", max_tokens=999)  # 1+999 == window


async def test_over_window_blocked_before_upstream(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "writer"))["key"]
        resp = await env.client.post("/v1/chat/completions", json=OVER, headers=auth(key))
        assert resp.status_code == 400
        message = resp.json()["error"]["message"]
        # OpenAI-style wording with the actual numbers, and the guardrail
        # attribution.
        assert "maximum context length is 1000 tokens" in message
        assert "5001 total" in message
        assert "guardrail 'context_window'" in message

        # Blocked pre-flight: the provider never saw it, nothing billed.
        assert [r for r in env.mock.requests if "messages" in r["body"]] == []
        logs = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"]
        assert logs[0]["status"] == "blocked"
        assert logs[0]["cost_usd"] == 0
        assert logs[0]["guardrail_actions"][0]["guardrail"] == "context_window"
        assert logs[0]["guardrail_actions"][0]["details"]["window"] == 1000


async def test_fitting_request_passes(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "fitter"))["key"]
        # 1 token in + 999 max_tokens == the 1000 window: fits (strictly
        # greater blocks).
        resp = await env.client.post("/v1/chat/completions", json=FITS, headers=auth(key))
        assert resp.status_code == 200
        assert len([r for r in env.mock.requests if "messages" in r["body"]]) == 1


async def test_reserve_output_tokens_when_max_tokens_absent(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "chatty"))["key"]
        # No max_tokens: the default reserve (1024) is assumed -> over.
        no_cap = chat_body(model="small-model", content="Hello")
        resp = await env.client.post("/v1/chat/completions", json=no_cap, headers=auth(key))
        assert resp.status_code == 400
        assert "assumed 1024 output tokens" in resp.json()["error"]["message"]

    # A smaller reserve leaves room: 1 + 500 <= 1000.
    async for env in _make_env(
        tmp_path, config=_config(reserve_output_tokens=500)
    ):
        key = (await create_key(env.client, "reserved"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="small-model", content="Hello"),
            headers=auth(key),
        )
        assert resp.status_code == 200


async def test_unmanaged_model_passes(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "free"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="unmanaged-model", content="Hello", max_tokens=999999),
            headers=auth(key),
        )
        assert resp.status_code == 200  # no declared window: unmanaged

    # default_window governs models that declare none.
    async for env in _make_env(
        tmp_path, config=_config(default_window=1000)
    ):
        key = (await create_key(env.client, "defaulted"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="unmanaged-model", content="Hello", max_tokens=5000),
            headers=auth(key),
        )
        assert resp.status_code == 400


async def test_window_param_overrides_model_config(tmp_path):
    async for env in _make_env(
        tmp_path, config=_config(window=500, on_exceed="block")
    ):
        key = (await create_key(env.client, "override"))["key"]
        # The model declares 1000; the param forces 500: 1 + 400 fits,
        # 1 + 600 does not.
        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="small-model", content="Hello", max_tokens=400),
            headers=auth(key),
        )
        assert ok.status_code == 200
        blocked = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="small-model", content="Hello", max_tokens=600),
            headers=auth(key),
        )
        assert blocked.status_code == 400
        assert "maximum context length is 500 tokens" in blocked.json()["error"]["message"]


async def test_redirect_to_bigger_model(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=_config(on_exceed="redirect", redirect_model="big-model"),
    ):
        key = (await create_key(env.client, "redirected"))["key"]
        resp = await env.client.post("/v1/chat/completions", json=OVER, headers=auth(key))
        assert resp.status_code == 200
        # Served by the big model: header + upstream + log all agree.
        assert resp.headers["X-Gateway-Model"] == "big-model"
        sent = [r for r in env.mock.requests if "messages" in r["body"]]
        assert [b["body"]["model"] for b in sent] == ["model-b"]
        logs = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"]
        assert logs[0]["model"] == "big-model"
        action = logs[0]["guardrail_actions"][0]
        assert action["guardrail"] == "context_window"
        assert action["action"] == "modify"


async def test_redirect_rechecks_model_permission(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=_config(on_exceed="redirect", redirect_model="big-model"),
    ):
        # A key restricted to the small model: the redirect target is not
        # allowed, and 4.5 fails closed rather than bypass permissions.
        # The refusal is a 404 model_not_found (the established
        # ModelNotAllowedError contract — it doesn't leak whether the
        # target exists).
        key = (
            await create_key(env.client, "restricted", allowed_models=["small-model"])
        )["key"]
        resp = await env.client.post("/v1/chat/completions", json=OVER, headers=auth(key))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"
        assert [r for r in env.mock.requests if "messages" in r["body"]] == []


async def test_stream_request_blocked_before_dispatch(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "streamer"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="small-model", content="Hello", max_tokens=5000, stream=True),
            headers=auth(key),
        )
        # Blocked pre-dispatch: the 400 envelope, not an SSE body.
        assert resp.status_code == 400
        assert "maximum context length" in resp.json()["error"]["message"]
        assert [r for r in env.mock.requests if "messages" in r["body"]] == []


async def test_native_surface_blocked(tmp_path):
    from .test_native_anthropic import anthropic_body

    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "anthropic-user"))["key"]
        resp = await env.client.post(
            "/v1/messages",
            json=anthropic_body("hello", model="small-model", max_tokens=5000),
            headers={"x-api-key": key},
        )
        assert resp.status_code == 400
        assert "maximum context length" in resp.json()["error"]["message"]
        assert [r for r in env.mock.requests if "messages" in r["body"]] == []


async def test_managed_system_prompt_counts_toward_window(tmp_path):
    # The estimate sees the OUTGOING message list: a managed system
    # prompt big enough to blow the window blocks a tiny request.
    async for env in _make_env(
        tmp_path,
        config=make_config(
            models=_models(),
            cache=CacheConfig(enabled=False),
            prompts=PromptsConfig(system_prompt="You are thorough. " * 400),
            guardrails=[
                GuardrailConfig(name="context_window", enabled=True, params={})
            ],
        ),
    ):
        key = (await create_key(env.client, "prompted"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="small-model", content="hi"),
            headers=auth(key),
        )
        assert resp.status_code == 400


async def test_observe_mode_flows_on(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            models=_models(),
            cache=CacheConfig(enabled=False),
            guardrails=[
                GuardrailConfig(
                    name="context_window", enabled=True, mode="observe", params={}
                )
            ],
        ),
    ):
        key = (await create_key(env.client, "observer"))["key"]
        resp = await env.client.post("/v1/chat/completions", json=OVER, headers=auth(key))
        # Observe: the verdict is logged and counted, the request flows on.
        assert resp.status_code == 200
        logs = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"]
        assert logs[0]["status"] == "success"
        assert logs[0]["guardrail_actions"][0]["guardrail"] == "context_window"


async def test_admin_routing_surfaces_windows(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        routing = (
            await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))
        ).json()
        windows = {m["name"]: m["context_window"] for m in routing["models"]}
        assert windows == {
            "small-model": 1000,
            "big-model": 100_000,
            "unmanaged-model": None,
        }


async def test_hot_reload_picks_up_window_changes(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "reloader"))["key"]
        assert (
            await env.client.post("/v1/chat/completions", json=FITS, headers=auth(key))
        ).status_code == 200

        # Shrink the window on the live config: the same request now
        # blocks (the pipeline's model-window map reloads with it).
        smaller = _config()
        smaller.models[0].context_window = 500
        env.app.state.pipeline.apply_config(smaller)
        blocked = await env.client.post(
            "/v1/chat/completions", json=FITS, headers=auth(key)
        )
        assert blocked.status_code == 400
        assert "maximum context length is 500 tokens" in blocked.json()["error"]["message"]


# -- unit-level ---------------------------------------------------------------

async def test_unit_window_from_ctx_and_bad_params():
    from gateway.core.context import AuthContext, RequestContext
    from gateway.schemas import ChatCompletionRequest

    guardrail = ContextWindowGuardrail({})
    ctx = RequestContext(request_id="r", auth=AuthContext(), context_window=1000)
    request = ChatCompletionRequest(
        model="small-model",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=5000,
    )
    result = await guardrail.check_request(request, ctx)
    assert result.action.value == "block"
    assert result.details == {
        "estimated_input_tokens": 1,
        "output_tokens": 5000,
        "window": 1000,
    }

    # No window anywhere: unmanaged, passes.
    bare = RequestContext(request_id="r", auth=AuthContext())
    assert await guardrail.check_request(request, bare) is None

    with pytest.raises(ValueError):
        ContextWindowGuardrail({"on_exceed": "redirect"})  # missing redirect_model
    with pytest.raises(ValueError):
        ContextWindowGuardrail({"on_exceed": "nonsense"})
