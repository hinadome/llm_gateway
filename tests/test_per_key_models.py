"""Tests for per-key model remapping (model_mappings / default_model).

A virtual key can carry its own routing table: ``model_mappings`` maps
requested names to public models, applied *before* the global
``model_aliases`` — and winning even over real public model names, because
an explicit admin decision for one key is exactly what per-team steering
means. ``default_model`` is that key's catch-all, beating the global one.

Everything downstream of resolution (permissions, rate limits, guardrail
model scopes, cache identity, logs, the echoed response "model") sees the
resolved public name; the client's original name survives in the log row's
``request_body`` — the same invariants global aliasing established.
"""

from __future__ import annotations

from gateway.config import DeploymentConfig, ModelConfig
from gateway.core.context import AuthContext
from gateway.core.pipeline import GatewayPipeline

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

# ----------------------------------------------------------------------
# Units: the resolution precedence
# ----------------------------------------------------------------------


async def test_resolution_precedence_matrix(tmp_path):
    """Per-key mappings win over everything; the rest follows global rules.

    Order: key mapping → real public name → global alias → key default →
    global default. Passing ``auth=None`` falls back to the pre-existing
    global-only behavior (which test_model_aliasing.py covers separately).
    """
    config = make_config(
        model_aliases={"gpt-4o": "test-model", "shadow": "other-model"},
        default_model="other-model",
    )
    async for env in _make_env(tmp_path, config=config):
        pipeline: GatewayPipeline = env.app.state.pipeline
        steered = AuthContext(
            model_mappings={"test-model": "other-model", "shadow": "test-model"},
            default_model="test-model",
        )
        # 1. key mapping beats a real public name and a global alias
        assert pipeline._resolve_model("test-model", steered) == "other-model"
        assert pipeline._resolve_model("shadow", steered) == "test-model"
        # 2. a real public name beats the key's default catch-all
        assert pipeline._resolve_model("other-model", steered) == "other-model"
        # 3. global alias (mapping miss)
        assert pipeline._resolve_model("gpt-4o", steered) == "test-model"
        # 4. key default beats the global default
        assert pipeline._resolve_model("totally-unknown", steered) == "test-model"
        # auth=None: global rules only, unchanged
        assert pipeline._resolve_model("test-model") == "test-model"
        assert pipeline._resolve_model("gpt-4o") == "test-model"
        assert pipeline._resolve_model("totally-unknown") == "other-model"
        # no key default → global default applies for that key too
        plain = AuthContext(model_mappings={"legacy": "test-model"})
        assert pipeline._resolve_model("totally-unknown", plain) == "other-model"
        assert pipeline._resolve_model("legacy", plain) == "test-model"
        break


# ----------------------------------------------------------------------
# e2e: chat
# ----------------------------------------------------------------------


async def test_mapping_routes_and_echoes_resolved_model(tmp_path):
    async for env in _make_env(tmp_path):
        key = (
            await create_key(
                env.client, "steered", model_mappings={"team-model": "other-model"}
            )
        )["key"]

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="team-model", content="steer me"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "other-model"  # resolved name echoed
        assert resp.headers["x-gateway-model"] == "other-model"
        assert resp.headers["x-gateway-deployment"] == "openai/other"
        # the upstream saw the target's native model, not the requested name
        assert env.mock.requests[-1]["body"]["model"] == "model-c"

        # the log records what actually ran; the request body keeps what
        # was asked (same contract as global aliases)
        request_id = resp.headers["x-request-id"]
        logs = await env.client.get(
            "/admin/logs", params={"request_id": request_id}, headers=auth(ADMIN_KEY)
        )
        entry = logs.json()["data"][0]
        assert entry["model"] == "other-model"
        detail = await env.client.get(
            f"/admin/logs/{entry['id']}", headers=auth(ADMIN_KEY)
        )
        assert detail.json()["request_body"]["model"] == "team-model"
        break


async def test_mapping_beats_real_name_e2e(tmp_path):
    """A key-specific mapping wins even over a real public model name —
    and composes with global aliases (which lose to the mapping)."""
    config = make_config(model_aliases={"gpt-4o": "test-model"})
    async for env in _make_env(tmp_path, config=config):
        key = (
            await create_key(
                env.client, "overrider", model_mappings={"test-model": "other-model"}
            )
        )["key"]

        direct = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="test-model", content="one"),
            headers=auth(key),
        )
        assert direct.status_code == 200
        assert direct.json()["model"] == "other-model"
        assert direct.headers["x-gateway-deployment"] == "openai/other"

        # global alias resolves to test-model — but the key maps that
        # resolved name on to other-model? No: the mapping is keyed by the
        # *requested* name. "gpt-4o" is not in the mapping, so the alias
        # applies and serves test-model.
        via_alias = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="gpt-4o", content="two"),
            headers=auth(key),
        )
        assert via_alias.status_code == 200
        assert via_alias.json()["model"] == "test-model"
        break


async def test_per_key_default_beats_global_default(tmp_path):
    config = make_config(default_model="test-model")
    async for env in _make_env(tmp_path, config=config):
        global_defaulted = (
            await create_key(env.client, "global-defaulted")
        )["key"]
        steered = (
            await create_key(env.client, "steered", default_model="other-model")
        )["key"]

        unknown = chat_body(model="whatever-this-is", content="catch me")
        a = await env.client.post(
            "/v1/chat/completions", json=unknown, headers=auth(global_defaulted)
        )
        assert a.status_code == 200
        assert a.json()["model"] == "test-model"  # global default

        b = await env.client.post(
            "/v1/chat/completions", json=unknown, headers=auth(steered)
        )
        assert b.status_code == 200
        assert b.json()["model"] == "other-model"  # key default wins
        break


async def test_permissions_match_resolved_model(tmp_path):
    async for env in _make_env(tmp_path):
        # target allowed → mapped request passes
        ok_key = (
            await create_key(
                env.client,
                "ok",
                allowed_models=["other-*"],
                model_mappings={"team-model": "other-model"},
            )
        )["key"]
        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="team-model", content="allowed"),
            headers=auth(ok_key),
        )
        assert ok.status_code == 200

        # requested name matches the allowlist but the *target* doesn't
        # → denied (the check sees the resolved name)
        denied_key = (
            await create_key(
                env.client,
                "denied",
                allowed_models=["team-*"],
                model_mappings={"team-model": "other-model"},
            )
        )["key"]
        denied = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="team-model", content="denied"),
            headers=auth(denied_key),
        )
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "model_not_found"
        break


async def test_mapping_shares_cache_with_direct_request(tmp_path):
    async for env in _make_env(tmp_path):
        key = (
            await create_key(
                env.client, "cacher", model_mappings={"team-model": "test-model"}
            )
        )["key"]

        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="test-model", content="cache me"),
            headers=auth(key),
        )
        assert first.headers["x-gateway-cache"] == "MISS"

        # same request via the mapped name → same resolved request → HIT
        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="team-model", content="cache me"),
            headers=auth(key),
        )
        assert second.status_code == 200
        assert second.headers["x-gateway-cache"] == "HIT"
        assert second.json()["model"] == "test-model"
        break


async def test_mapping_to_unknown_target_404s(tmp_path):
    """No admin-time target validation (like global aliases): a broken
    target surfaces as a 404 that names the target."""
    async for env in _make_env(tmp_path):
        key = (
            await create_key(
                env.client,
                "broken",
                model_mappings={"team-model": "not-a-real-model"},
            )
        )["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="team-model", content="hi"),
            headers=auth(key),
        )
        assert resp.status_code == 404
        body = resp.json()["error"]
        assert body["code"] == "model_not_found"
        assert "not-a-real-model" in body["message"]
        break


# ----------------------------------------------------------------------
# e2e: admin API round-trip
# ----------------------------------------------------------------------


async def test_admin_create_patch_round_trip(tmp_path):
    async for env in _make_env(tmp_path):
        created = await create_key(
            env.client,
            "roundtrip",
            model_mappings={"team-model": "other-model"},
            default_model="test-model",
        )
        assert created["model_mappings"] == {"team-model": "other-model"}
        assert created["default_model"] == "test-model"

        # update both fields
        patched = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"model_mappings": {"team-model": "test-model"}},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["model_mappings"] == {"team-model": "test-model"}
        assert patched.json()["default_model"] == "test-model"  # untouched

        # clearing: explicit null in the body
        cleared = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"model_mappings": None, "default_model": None},
            headers=auth(ADMIN_KEY),
        )
        assert cleared.status_code == 200
        assert cleared.json()["model_mappings"] is None
        assert cleared.json()["default_model"] is None

        # and the behavior follows: the mapped name now resolves to nothing
        key = created["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="team-model", content="cleared"),
            headers=auth(key),
        )
        assert resp.status_code == 404
        break


# ----------------------------------------------------------------------
# e2e: embeddings + /v1/models
# ----------------------------------------------------------------------


async def test_embeddings_mapping(tmp_path):
    config = make_config(
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
                name="text-embedding-mock",
                deployments=[
                    DeploymentConfig(
                        name="openai/emb", provider="openai", model="embed-mock-a"
                    )
                ],
            ),
        ]
    )
    async for env in _make_env(tmp_path, config=config):
        key = (
            await create_key(
                env.client,
                "emb-team",
                model_mappings={"team-embed": "text-embedding-mock"},
            )
        )["key"]
        resp = await env.client.post(
            "/v1/embeddings",
            json={"model": "team-embed", "input": "hello"},
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "text-embedding-mock"
        assert env.mock.requests[-1]["body"]["model"] == "embed-mock-a"
        assert env.mock.requests[-1]["url"].endswith("/embeddings")
        break


async def test_models_listing_and_retrieve_with_mappings(tmp_path):
    async for env in _make_env(tmp_path):
        key = (
            await create_key(
                env.client, "lister", model_mappings={"gpt-4o": "test-model"}
            )
        )["key"]
        listed = await env.client.get("/v1/models", headers=auth(key))
        ids = [m["id"] for m in listed.json()["data"]]
        assert "test-model" in ids  # the public model
        assert "gpt-4o" in ids  # plus the name this key may send

        # retrieve echoes the asked name (like global aliases)
        ok = await env.client.get("/v1/models/gpt-4o", headers=auth(key))
        assert ok.status_code == 200
        assert ok.json()["id"] == "gpt-4o"

        # a mapping whose target the key may not use is not exposed
        restricted = (
            await create_key(
                env.client,
                "restricted",
                allowed_models=["test-*"],
                model_mappings={"secret-codename": "other-model"},
            )
        )["key"]
        r_listed = await env.client.get("/v1/models", headers=auth(restricted))
        r_ids = [m["id"] for m in r_listed.json()["data"]]
        assert "test-model" in r_ids
        assert "other-model" not in r_ids  # allowlist
        assert "secret-codename" not in r_ids  # target not permitted

        denied = await env.client.get(
            "/v1/models/secret-codename", headers=auth(restricted)
        )
        assert denied.status_code == 404
        break
