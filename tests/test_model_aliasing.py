"""Tests for model aliasing and default-model routing.

``model_aliases`` maps client-requested names to public model names, so
apps with hardcoded provider model names work against the gateway without
code changes; ``default_model`` (opt-in) catches any unknown name.
Everything downstream of resolution — permissions, cache identity,
guardrail model scopes, logs, the echoed response "model" — sees the
canonical public name; the client's original name survives in the log
row's ``request_body``.
"""

from __future__ import annotations

from gateway.config import GatewayConfig
from gateway.core.pipeline import GatewayPipeline

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

# ----------------------------------------------------------------------


def test_public_models_includes_aliases():
    config = GatewayConfig.model_validate(
        {
            "models": [{"name": "gpt-4o", "deployments": []}],
            "model_aliases": {
                "gpt-4o": "gpt-4o",  # shadowed by the real model
                "gpt4o-alias": "gpt-4o",
            },
        }
    )
    assert config.public_models() == ["gpt-4o", "gpt4o-alias"]


async def test_alias_resolution_rules(tmp_path):
    """Real model wins; then alias; then default_model; else unchanged."""
    config = make_config(
        model_aliases={"gpt-4o": "test-model", "shadow": "other-model"},
        default_model="other-model",
    )
    async for env in _make_env(tmp_path, config=config):
        pipeline: GatewayPipeline = env.app.state.pipeline
        assert pipeline._resolve_model("test-model") == "test-model"  # real name
        assert pipeline._resolve_model("gpt-4o") == "test-model"  # alias
        assert pipeline._resolve_model("shadow") == "other-model"  # alias
        assert pipeline._resolve_model("totally-unknown") == "other-model"  # default
        break


async def test_no_default_unknown_passes_through(tmp_path):
    config = make_config()
    async for env in _make_env(tmp_path, config=config):
        pipeline: GatewayPipeline = env.app.state.pipeline
        assert pipeline._resolve_model("no-such-model") == "no-such-model"
        break


# ----------------------------------------------------------------------
# e2e: chat
# ----------------------------------------------------------------------


async def test_alias_request_routes_and_echoes_canonical(tmp_path):
    config = make_config(model_aliases={"gpt-4o": "test-model"})
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "legacy-app"))["key"]

        direct = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="direct request"),
            headers=auth(key),
        )
        assert direct.status_code == 200
        assert direct.json()["model"] == "test-model"

        via_alias = await env.client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "alias request"}]},
            headers=auth(key),
        )
        assert via_alias.status_code == 200
        assert via_alias.json()["model"] == "test-model"  # canonical name echoed
        assert via_alias.headers["x-gateway-model"] == "test-model"
        # the upstream saw the deployment's native model
        assert env.mock.requests[-1]["body"]["model"] == "model-a"
        break


async def test_alias_shares_cache_with_direct_name(tmp_path):
    config = make_config(model_aliases={"gpt-4o": "test-model"})
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "cacher"))["key"]
        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="cache me"),
            headers=auth(key),
        )
        assert first.headers["x-gateway-cache"] == "MISS"
        # same request via the alias -> same normalized (resolved) request
        second = await env.client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "cache me"}]},
            headers=auth(key),
        )
        assert second.status_code == 200
        assert second.headers["x-gateway-cache"] == "HIT"
        break


async def test_alias_in_models_endpoints(tmp_path):
    config = make_config(model_aliases={"gpt-4o": "test-model"})
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "lister"))["key"]
        listed = await env.client.get("/v1/models", headers=auth(key))
        ids = [m["id"] for m in listed.json()["data"]]
        assert "gpt-4o" in ids and "test-model" in ids

        ok = await env.client.get("/v1/models/gpt-4o", headers=auth(key))
        assert ok.status_code == 200
        assert ok.json()["id"] == "gpt-4o"

        # unknown names still 404 (no default_model configured)
        missing = await env.client.get("/v1/models/nope", headers=auth(key))
        assert missing.status_code == 404
        break


async def test_permissions_apply_to_resolved_model(tmp_path):
    config = make_config(model_aliases={"gpt-4o": "test-model"})
    async for env in _make_env(tmp_path, config=config):
        canonical_only = (
            await create_key(env.client, "canon", allowed_models=["test-*"])
        )["key"]
        alias_only = (
            await create_key(env.client, "aliasy", allowed_models=["gpt-*"])
        )["key"]

        # canonical pattern -> alias works (resolved name matches)
        ok = await env.client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(canonical_only),
        )
        assert ok.status_code == 200

        # alias pattern only -> denied: the check sees the resolved name
        denied = await env.client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(alias_only),
        )
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "model_not_found"
        break


async def test_log_preserves_original_name(tmp_path):
    config = make_config(model_aliases={"gpt-4o": "test-model"})
    async for env in _make_env(tmp_path, config=config):
        await env.client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "logged"}]},
            headers=auth(ADMIN_KEY),
        )
        logs = await env.client.get("/admin/logs?limit=1", headers=auth(ADMIN_KEY))
        entry = logs.json()["data"][0]
        assert entry["model"] == "test-model"  # what actually ran
        detail = await env.client.get(
            f"/admin/logs/{entry['id']}", headers=auth(ADMIN_KEY)
        )
        assert detail.json()["request_body"]["model"] == "gpt-4o"  # what was asked
        break


# ----------------------------------------------------------------------
# e2e: default model + embeddings
# ----------------------------------------------------------------------


async def test_default_model_catches_unknown_names(tmp_path):
    config = make_config(default_model="test-model")
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "typo-prone"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="gpt-4o-turbo-preview", content="typo"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "test-model"
        assert resp.headers["x-gateway-model"] == "test-model"
        break


async def test_default_model_real_name_still_wins(tmp_path):
    config = make_config(default_model="other-model")
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "priority"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="real name beats default"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-model"] == "test-model"
        break


async def test_embedding_alias(tmp_path):
    config = make_config(model_aliases={"text-embedding-3-small": "test-model"})
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "embedder"))["key"]
        resp = await env.client.post(
            "/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": "hello"},
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "test-model"
        break
