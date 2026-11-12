"""Tests for token scoping: per-key allowed_endpoints + allowed_ips.

Two restrictions a virtual key can carry (admin API/UI, no restart):
which parts of the data plane it may touch — ``chat`` covers every
chat-shaped surface (OpenAI chat, the Responses bridge, native
Anthropic/Gemini formats), ``embeddings`` and ``models`` the rest — and
which source addresses calls may come from (plain IPs or CIDR blocks).
``None`` (or an explicit-null PATCH) means unrestricted. Both are
enforced after the auth chain succeeds, never against admin contexts,
and both apply identically to raw-key and JWT callers.
"""

from __future__ import annotations

import time

import jwt as pyjwt
from sqlalchemy import update

from gateway.api.deps import _ip_allowed, required_scope
from gateway.config import AuthConfig, JWTAuthConfig
from gateway.db import get_sessionmaker
from gateway.models import VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

NOW = int(time.time())
SECRET = "test-shared-secret"


def embeddings_body(text: str = "hi") -> dict:
    return {"model": "test-model", "input": text}


def anthropic_body(content: str = "hello") -> dict:
    return {
        "model": "test-model",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": content}],
    }


def gemini_body(text: str = "hello") -> dict:
    return {"contents": [{"role": "user", "parts": [{"text": text}]}]}


def responses_body() -> dict:
    return {"model": "test-model", "input": "hello there"}


# ----------------------------------------------------------------------
# Pure functions: path -> scope, IP matching
# ----------------------------------------------------------------------


def test_required_scope_mapping():
    # chat-shaped surfaces (OpenAI chat, Responses bridge, native formats)
    assert required_scope("/v1/chat/completions") == "chat"
    assert required_scope("/v1/responses") == "chat"
    assert required_scope("/v1/messages") == "chat"
    assert required_scope("/v1/models/test-model:generateContent") == "chat"
    assert required_scope("/v1beta/models/test-model:generateContent") == "chat"
    assert required_scope("/v1/models/test-model:streamGenerateContent") == "chat"
    # model objects (no :method suffix)
    assert required_scope("/v1/models") == "models"
    assert required_scope("/v1/models/test-model") == "models"
    # embeddings
    assert required_scope("/v1/embeddings") == "embeddings"
    # everything else is the admin plane / metrics / docs — unscoped here
    assert required_scope("/admin/keys") is None
    assert required_scope("/metrics") is None


def test_ip_allowed_matrix():
    assert _ip_allowed("10.1.2.3", ["10.1.2.3"])  # plain match
    assert _ip_allowed("10.1.2.3", ["10.0.0.0/8"])  # CIDR
    assert _ip_allowed("10.1.2.3", ["192.168.1.1", "10.0.0.0/8"])  # any entry
    assert not _ip_allowed("10.1.2.4", ["10.1.2.3"])  # no match
    assert not _ip_allowed("192.168.0.1", ["10.0.0.0/8"])
    # malformed entries restrict rather than open (and never crash)
    assert not _ip_allowed("10.1.2.3", ["not-an-ip"])
    assert _ip_allowed("10.1.2.3", ["not-an-ip", "10.0.0.0/8"])
    # no client address + a restriction set = denied
    assert not _ip_allowed(None, ["10.0.0.0/8"])
    assert not _ip_allowed("garbage-host", ["10.0.0.0/8"])
    # IPv6 works too
    assert _ip_allowed("2001:db8::1", ["2001:db8::/32"])
    assert not _ip_allowed("2001:db8::1", ["2001:db9::/32"])


# ----------------------------------------------------------------------
# Endpoint scoping, end to end
# ----------------------------------------------------------------------


async def _scoped_env(tmp_path, **key_fields):
    """App + a key with the given scoping fields; returns (env, key)."""
    async for env in _make_env(tmp_path):
        created = await create_key(env.client, "scoped", **key_fields)
        yield env, created


async def test_chat_only_scope_covers_chat_surfaces(tmp_path):
    async for env, key in _scoped_env(tmp_path, allowed_endpoints=["chat"]):
        headers = auth(key["key"])
        # every chat-shaped surface passes
        assert (
            await env.client.post(
                "/v1/chat/completions", json=chat_body(content="hi"), headers=headers
            )
        ).status_code == 200
        assert (
            await env.client.post("/v1/messages", json=anthropic_body(), headers=headers)
        ).status_code == 200
        assert (
            await env.client.post(
                "/v1/models/test-model:generateContent",
                json=gemini_body(),
                headers={"x-goog-api-key": key["key"]},
            )
        ).status_code == 200
        assert (
            await env.client.post("/v1/responses", json=responses_body(), headers=headers)
        ).status_code == 200


async def test_chat_only_scope_blocks_embeddings_and_models(tmp_path):
    async for env, key in _scoped_env(tmp_path, allowed_endpoints=["chat"]):
        headers = auth(key["key"])
        denied = await env.client.post(
            "/v1/embeddings", json=embeddings_body(), headers=headers
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "endpoint_not_allowed"
        assert (
            await env.client.get("/v1/models", headers=headers)
        ).status_code == 403
        assert (
            await env.client.get("/v1/models/test-model", headers=headers)
        ).status_code == 403


async def test_embeddings_and_models_scope(tmp_path):
    async for env, key in _scoped_env(
        tmp_path, allowed_endpoints=["embeddings", "models"]
    ):
        headers = auth(key["key"])
        assert (
            await env.client.post(
                "/v1/embeddings", json=embeddings_body(), headers=headers
            )
        ).status_code == 200
        assert (await env.client.get("/v1/models", headers=headers)).status_code == 200
        assert (
            await env.client.get("/v1/models/test-model", headers=headers)
        ).status_code == 200
        denied = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=headers
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "endpoint_not_allowed"


async def test_no_scoping_by_default(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "free-range"))["key"]
        headers = auth(key)
        assert (
            await env.client.post(
                "/v1/chat/completions", json=chat_body(content="hi"), headers=headers
            )
        ).status_code == 200
        assert (
            await env.client.post("/v1/embeddings", json=embeddings_body(), headers=headers)
        ).status_code == 200
        assert (await env.client.get("/v1/models", headers=headers)).status_code == 200


async def test_patch_updates_and_clears_scoping(tmp_path):
    async for env in _make_env(tmp_path):
        created = await create_key(env.client, "scoped")
        key_id, headers = created["id"], auth(created["key"])

        # restrict to chat
        patched = await env.client.patch(
            f"/admin/keys/{key_id}",
            json={"allowed_endpoints": ["chat"]},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["allowed_endpoints"] == ["chat"]
        assert (
            await env.client.post(
                "/v1/embeddings", json=embeddings_body(), headers=headers
            )
        ).status_code == 403

        # explicit null lifts the restriction
        cleared = await env.client.patch(
            f"/admin/keys/{key_id}",
            json={"allowed_endpoints": None},
            headers=auth(ADMIN_KEY),
        )
        assert cleared.status_code == 200
        assert cleared.json()["allowed_endpoints"] is None
        assert (
            await env.client.post(
                "/v1/embeddings", json=embeddings_body(), headers=headers
            )
        ).status_code == 200


async def test_unknown_scope_names_rejected(tmp_path):
    async for env in _make_env(tmp_path):
        bad_create = await env.client.post(
            "/admin/keys",
            json={"name": "typo", "allowed_endpoints": ["cha"]},
            headers=auth(ADMIN_KEY),
        )
        assert bad_create.status_code == 400
        assert "cha" in bad_create.json()["error"]["message"]

        created = await create_key(env.client, "scoped")
        bad_patch = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"allowed_endpoints": ["chat", "chats"]},
            headers=auth(ADMIN_KEY),
        )
        assert bad_patch.status_code == 400
        assert "chats" in bad_patch.json()["error"]["message"]


# ----------------------------------------------------------------------
# IP scoping, end to end
# ----------------------------------------------------------------------


async def test_allowed_ips_enforced(tmp_path):
    # ASGITransport calls come from 127.0.0.1
    async for env in _make_env(tmp_path):
        exact = (await create_key(env.client, "ip-exact", allowed_ips=["127.0.0.1"]))["key"]
        cidr = (await create_key(env.client, "ip-cidr", allowed_ips=["127.0.0.0/8"]))["key"]
        elsewhere = (await create_key(env.client, "ip-elsewhere", allowed_ips=["10.0.0.0/8"]))["key"]

        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(exact),
        )
        assert ok.status_code == 200

        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(cidr),
        )
        assert ok.status_code == 200

        denied = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(elsewhere),
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ip_not_allowed"


# ----------------------------------------------------------------------
# Interactions: admin bypass, JWT callers, both restrictions at once
# ----------------------------------------------------------------------


async def test_admin_context_bypasses_scoping(tmp_path):
    """The restrictions guard the data plane, not administrators: a key
    promoted to admin ignores endpoint/IP scoping (the bootstrap admin
    key has no row and thus no restrictions at all)."""
    async for env in _make_env(tmp_path):
        created = await create_key(
            env.client,
            "admin-scoped",
            allowed_endpoints=["chat"],
            allowed_ips=["10.0.0.0/8"],
        )
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VirtualKey)
                .where(VirtualKey.name == "admin-scoped")
                .values(is_admin=True)
            )
            await session.commit()
        headers = auth(created["key"])
        assert (
            await env.client.post(
                "/v1/embeddings", json=embeddings_body(), headers=headers
            )
        ).status_code == 200
        assert (await env.client.get("/v1/models", headers=headers)).status_code == 200


async def test_scoping_applies_to_jwt_callers(tmp_path):
    """A JWT naming a scoped key inherits its restrictions — the key row
    is the single source of policy for every auth method."""
    config = make_config(
        auth=AuthConfig(
            chain=["admin_key", "jwt"],
            jwt=JWTAuthConfig(secret=SECRET),
        )
    )
    async for env in _make_env(tmp_path, config=config):
        await create_key(env.client, "jwt-scoped", allowed_endpoints=["chat"])
        token = pyjwt.encode(
            {"sub": "jwt-scoped", "exp": NOW + 600}, SECRET, algorithm="HS256"
        )
        assert (
            await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="hi"),
                headers=auth(token),
            )
        ).status_code == 200
        denied = await env.client.post(
            "/v1/embeddings", json=embeddings_body(), headers=auth(token)
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "endpoint_not_allowed"


async def test_endpoints_and_ips_compose(tmp_path):
    async for env, key in _scoped_env(
        tmp_path, allowed_endpoints=["chat"], allowed_ips=["10.0.0.0/8"]
    ):
        # IP denial fires regardless of the (allowed) endpoint scope
        denied = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(key["key"]),
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ip_not_allowed"
