"""BYOK vault: tenant-owned provider credentials, encrypted at rest.

The contract: a tenant admin stores a credential once (PUT
/admin/tenants/{id}/provider-keys/{provider}) and every request from
that tenant through that provider rides THEIR account instead of the
platform's configured key — chat (stream and not), embeddings, images,
through every retry and fallback leg, resolved PER CANDIDATE provider.
The raw key exists exactly twice: the caller's PUT body and the
upstream Authorization header. At rest it is Fernet-encrypted under
GATEWAY_VAULT_MASTER_KEY; the admin API speaks only a hint (last
characters); audit records carry the hint, never the key; log rows
record only that BYOK was used (the provider name).

Fail-closed is the load-bearing property: an entry that exists but
cannot be decrypted (a rotated-away master key) fails the request with
byok_key_unreadable — a tenant that deliberately brought its own key
must never silently spend the platform's. The complement is
byte-for-byte neutrality: without a master key the vault is disabled
(endpoints 400 honestly), and a caller with no stored key anywhere is
served exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from prometheus_client.registry import REGISTRY
from sqlalchemy import select

from gateway.config import (
    AuditConfig,
    DeploymentConfig,
    ModelConfig,
    ProviderConfig,
)
from gateway.db import get_sessionmaker
from gateway.models import ProviderKey

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

# One master key per test module run (Fernet keys are random; any
# valid one works — what matters is that the same key encrypts and
# decrypts within an env, and a DIFFERENT one cannot).
MASTER = Fernet.generate_key().decode()
MASTER2 = Fernet.generate_key().decode()
PLATFORM_KEY = "provider-key"  # make_config's default openai key


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _make_tenant(client, name: str) -> dict:
    resp = await client.post(
        "/admin/tenants", json={"name": name}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _put_provider_key(
    client, tenant_id: str, provider: str, key: str, label: str | None = None
) -> httpx.Response:
    return await client.put(
        f"/admin/tenants/{tenant_id}/provider-keys/{provider}",
        json={"key": key, "label": label},
        headers=auth(ADMIN_KEY),
    )


async def _last_log(client) -> dict:
    logs = (await client.get("/admin/logs", headers=auth(ADMIN_KEY))).json()["data"]
    return logs[0]


def _auths(env, model: str | None = None) -> list[str]:
    """Upstream Authorization headers seen by the mock (optionally only
    requests whose body model matches)."""
    out = []
    for req in env.mock.requests:
        body = req.get("body") or {}
        if model is not None and body.get("model") != model:
            continue
        out.append(req["headers"].get("authorization", ""))
    return out


# ----------------------------------------------------------------------
# The core swap: the tenant's key rides the upstream call
# ----------------------------------------------------------------------


async def test_tenant_key_rides_upstream_chat(tmp_path):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        member = (
            await create_key(env.client, "member", tenant_id=tenant["id"])
        )["key"]
        stored = "sk-acme-very-secret-1234"
        put = await _put_provider_key(
            env.client, tenant["id"], "openai", stored, label="acme-prod"
        )
        assert put.status_code == 200, put.text
        # Metadata only: the key itself is never echoed back, not even
        # to the caller that just sent it.
        meta = put.json()
        assert meta["key_hint"] == "…1234"
        assert "key" not in meta and "encrypted_key" not in meta

        byok_before = _sample("gateway_byok_requests_total", {"provider": "openai"})
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(member),
        )
        assert resp.status_code == 200, resp.text
        # THE assertion: the upstream call authenticated as the tenant.
        assert (
            env.mock.requests[-1]["headers"]["authorization"]
            == f"Bearer {stored}"
        )
        # Observability: header, log row, metric — provider name only.
        assert resp.headers["x-gateway-byok-provider"] == "openai"
        assert (await _last_log(env.client))["byok_provider"] == "openai"
        assert (
            _sample("gateway_byok_requests_total", {"provider": "openai"})
            == byok_before + 1
        )


async def test_platform_and_keyless_callers_unchanged(tmp_path):
    # Neutrality: enabling the vault changes nothing for anyone without
    # a stored key — platform keys, other tenants, byte for byte.
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        keyed = await _make_tenant(env.client, "keyed")
        other = await _make_tenant(env.client, "other")
        member = (
            await create_key(env.client, "member", tenant_id=keyed["id"])
        )["key"]
        other_member = (
            await create_key(env.client, "other-member", tenant_id=other["id"])
        )["key"]
        platform_user = (await create_key(env.client, "platform-user"))["key"]
        await _put_provider_key(env.client, keyed["id"], "openai", "sk-keyed-999")

        byok_before = _sample("gateway_byok_requests_total", {"provider": "openai"})
        for key in (platform_user, other_member):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content=f"hello from {key[:6]}"),
                headers=auth(key),
            )
            assert resp.status_code == 200
            # Platform credential, no byok header, no byok on the row.
            assert (
                env.mock.requests[-1]["headers"]["authorization"]
                == f"Bearer {PLATFORM_KEY}"
            )
            assert "x-gateway-byok-provider" not in resp.headers
            assert (await _last_log(env.client))["byok_provider"] is None
        assert _sample("gateway_byok_requests_total", {"provider": "openai"}) == (
            byok_before
        )

        # The keyed tenant's member still rides their stored key.
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(member)
        )
        assert resp.status_code == 200
        assert env.mock.requests[-1]["headers"]["authorization"] == "Bearer sk-keyed-999"


async def test_tenant_isolation(tmp_path):
    # Tenant A's stored key NEVER authenticates tenant B's traffic.
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        a = await _make_tenant(env.client, "acme")
        b = await _make_tenant(env.client, "globex")
        a_member = (await create_key(env.client, "a-member", tenant_id=a["id"]))["key"]
        b_member = (await create_key(env.client, "b-member", tenant_id=b["id"]))["key"]
        assert (
            await _put_provider_key(env.client, a["id"], "openai", "sk-acme-only-777")
        ).status_code == 200

        assert (
            await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="hello B"),
                headers=auth(b_member),
            )
        ).status_code == 200
        assert (
            env.mock.requests[-1]["headers"]["authorization"]
            == f"Bearer {PLATFORM_KEY}"
        )
        assert (
            await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="hello A"),
                headers=auth(a_member),
            )
        ).status_code == 200
        assert (
            env.mock.requests[-1]["headers"]["authorization"]
            == "Bearer sk-acme-only-777"
        )


# ----------------------------------------------------------------------
# Every modality and every failover leg
# ----------------------------------------------------------------------


async def test_stream_embeddings_and_images_swap_too(tmp_path):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        member = (
            await create_key(env.client, "member", tenant_id=tenant["id"])
        )["key"]
        stored = "sk-acme-multi-4242"
        await _put_provider_key(env.client, tenant["id"], "openai", stored)

        # Streaming chat: same credential on the wire, same header.
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stream me", stream=True),
            headers=auth(member),
        )
        assert resp.status_code == 200
        assert env.mock.requests[-1]["headers"]["authorization"] == f"Bearer {stored}"
        assert resp.headers["x-gateway-byok-provider"] == "openai"

        # Embeddings surface.
        resp = await env.client.post(
            "/v1/embeddings",
            json={"model": "test-model", "input": "hello"},
            headers=auth(member),
        )
        assert resp.status_code == 200
        assert env.mock.requests[-1]["headers"]["authorization"] == f"Bearer {stored}"
        assert resp.headers["x-gateway-byok-provider"] == "openai"

        # Images surface.
        resp = await env.client.post(
            "/v1/images/generations",
            json={"model": "test-model", "prompt": "a cat", "n": 1},
            headers=auth(member),
        )
        assert resp.status_code == 200, resp.text
        assert env.mock.requests[-1]["headers"]["authorization"] == f"Bearer {stored}"


async def test_fallback_legs_resolve_per_candidate_provider(tmp_path):
    # The credential is resolved PER CANDIDATE inside the failover loop:
    # the tenant keyed only openai; the openai leg exhausts its retries
    # with THEIR key, the azure fallback leg rides the PLATFORM's azure
    # key (override semantics: per provider), and the row records no
    # byok for the leg that actually served.
    config = make_config(
        providers={
            "openai": ProviderConfig(
                api_base="http://mockupstream.test/v1", api_key="platform-openai"
            ),
            "azure": ProviderConfig(
                # Azure builds deployment-addressed paths itself
                # (/openai/deployments/{name}/...), so no /v1 suffix —
                # and authenticates with the api-key header, not Bearer.
                api_base="http://mockupstream.test",
                api_key="platform-azure",
                api_version="2024-02-01",
            ),
        },
        models=[
            ModelConfig(
                name="dual",
                deployments=[
                    DeploymentConfig(
                        name="openai/leg", provider="openai", model="model-a"
                    ),
                    DeploymentConfig(
                        name="azure/leg",
                        provider="azure",
                        model="model-b",
                        priority=200,
                    ),
                ],
            ),
        ],
        pricing={
            "model-a": {"input": 2.0, "output": 4.0},
            "model-b": {"input": 2.0, "output": 4.0},
        },
    )
    async for env in _make_env(tmp_path, config=config, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        member = (
            await create_key(env.client, "member", tenant_id=tenant["id"])
        )["key"]
        await _put_provider_key(env.client, tenant["id"], "openai", "sk-acme-openai")

        env.mock.fail_counts["model-a"] = 3  # exhaust the openai leg
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="fail over", model="dual"),
            headers=auth(member),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-deployment"] == "azure/leg"

        # Every model-a attempt authenticated as the TENANT; the azure
        # leg (the one that served) as the PLATFORM — via the api-key
        # header, which is how Azure authenticates.
        assert _auths(env, model="model-a") == ["Bearer sk-acme-openai"] * 3
        azure_legs = [
            req["headers"].get("api-key", "")
            for req in env.mock.requests
            if (req.get("body") or {}).get("model") == "model-b"
        ]
        assert azure_legs == ["platform-azure"]
        # The serving leg had no stored key: no byok marker anywhere.
        assert "x-gateway-byok-provider" not in resp.headers
        assert (await _last_log(env.client))["byok_provider"] is None


# ----------------------------------------------------------------------
# Encryption at rest
# ----------------------------------------------------------------------


async def test_encrypted_at_rest_never_plaintext(tmp_path):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        stored = "sk-atrest-secret-9911"
        put = await _put_provider_key(env.client, tenant["id"], "openai", stored)
        assert put.status_code == 200

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(ProviderKey).where(ProviderKey.tenant_id == tenant["id"])
            )
            row = result.scalar_one()
        # Ciphertext in the row, and it decrypts back to the original
        # under the master key — the round trip that serving uses.
        assert row.encrypted_key != stored
        assert stored not in row.encrypted_key
        fernet = Fernet(MASTER)
        assert fernet.decrypt(row.encrypted_key.encode()).decode() == stored
        assert row.key_hint == "…9911"

        # The LIST surface carries the hint, never the key — scan the
        # serialized response wholesale so nothing slips through.
        listing = await env.client.get(
            f"/admin/tenants/{tenant['id']}/provider-keys", headers=auth(ADMIN_KEY)
        )
        assert listing.status_code == 200
        assert stored not in listing.text
        assert stored not in put.text


# ----------------------------------------------------------------------
# Fail closed: undecryptable entries never silently bill the platform
# ----------------------------------------------------------------------


async def test_undecryptable_entry_fails_closed(tmp_path):
    # Two vaults over ONE database: the entry was stored under MASTER,
    # the process restarts with MASTER2 — decrypt fails, and the
    # request 500s byok_key_unreadable instead of silently spending the
    # platform's key. Platform callers keep serving (the corruption is
    # scoped to the tenant+provider that owns the entry).
    async for env1 in _make_env(tmp_path, vault_master_key=MASTER):
        tenant = await _make_tenant(env1.client, "acme")
        member = (await create_key(env1.client, "member", tenant_id=tenant["id"]))["key"]
        assert (
            await _put_provider_key(env1.client, tenant["id"], "openai", "sk-rotated-0001")
        ).status_code == 200
        # The entry was GOOD before the rotation: the member rides it.
        assert (
            await env1.client.post(
                "/v1/chat/completions",
                json=chat_body(content="hello"),
                headers=auth(member),
            )
        ).status_code == 200
        assert (
            env1.mock.requests[-1]["headers"]["authorization"]
            == "Bearer sk-rotated-0001"
        )

    # Same tmp_path → the SAME SQLite file: the tenant and its (still
    # MASTER-encrypted) vault entry persist; the process comes back
    # with MASTER2 only.
    async for env in _make_env(tmp_path, vault_master_key=MASTER2):
        tenants = (await env.client.get("/admin/tenants", headers=auth(ADMIN_KEY))).json()
        acme = next(t for t in tenants["data"] if t["name"] == "acme")
        member = (await create_key(env.client, "member2", tenant_id=acme["id"]))["key"]

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(member),
        )
        assert resp.status_code == 500
        err = resp.json()["error"]
        assert err["code"] == "byok_key_unreadable"
        assert "re-store" in err["message"]

        # Nothing reached the upstream under any credential.
        assert _auths(env) == []

        # A platform caller is unaffected by the tenant's bad entry.
        platform_user = (await create_key(env.client, "platform-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(platform_user),
        )
        assert resp.status_code == 200
        assert env.mock.requests[-1]["headers"]["authorization"] == (
            f"Bearer {PLATFORM_KEY}"
        )


# ----------------------------------------------------------------------
# Admin API: lifecycle, authz, disabled-vault honesty
# ----------------------------------------------------------------------


async def test_vault_disabled_answers_honestly(tmp_path):
    # No master key: PUT refuses with a 400 naming exactly what is
    # missing (never a silent plaintext store), reads stay honest.
    async for env in _make_env(tmp_path):
        tenant = await _make_tenant(env.client, "acme")
        put = await _put_provider_key(env.client, tenant["id"], "openai", "sk-nope")
        assert put.status_code == 400
        err = put.json()["error"]
        assert err["code"] == "vault_unavailable"
        assert "GATEWAY_VAULT_MASTER_KEY" in err["message"]

        listing = await env.client.get(
            f"/admin/tenants/{tenant['id']}/provider-keys", headers=auth(ADMIN_KEY)
        )
        assert listing.status_code == 200
        assert listing.json()["data"] == []


async def test_admin_lifecycle_upsert_and_delete(tmp_path):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        member = (
            await create_key(env.client, "member", tenant_id=tenant["id"])
        )["key"]
        tid = tenant["id"]

        # Store → the request rides the new key immediately (the write
        # invalidates the local cache).
        put = await _put_provider_key(env.client, tid, "openai", "sk-first-1111")
        assert put.status_code == 200
        assert (
            await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="one"),
                headers=auth(member),
            )
        ).status_code == 200
        assert env.mock.requests[-1]["headers"]["authorization"] == "Bearer sk-first-1111"

        # Replace → ONE row (unique per tenant+provider), the new key
        # serves the very next request, the hint follows.
        put2 = await _put_provider_key(env.client, tid, "openai", "sk-second-2222")
        assert put2.status_code == 200
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(ProviderKey).where(ProviderKey.tenant_id == tid)
            )
            rows = result.scalars().all()
        assert len(rows) == 1 and rows[0].key_hint == "…2222"
        assert (
            await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="two"),
                headers=auth(member),
            )
        ).status_code == 200
        assert (
            env.mock.requests[-1]["headers"]["authorization"]
            == "Bearer sk-second-2222"
        )

        # List → metadata only.
        listing = await env.client.get(
            f"/admin/tenants/{tid}/provider-keys", headers=auth(ADMIN_KEY)
        )
        (entry,) = listing.json()["data"]
        assert entry["provider"] == "openai"
        assert entry["key_hint"] == "…2222"
        assert "key" not in entry

        # Delete → the platform key returns within the same process.
        deleted = await env.client.delete(
            f"/admin/tenants/{tid}/provider-keys/openai", headers=auth(ADMIN_KEY)
        )
        assert deleted.status_code == 200
        assert (
            await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="three"),
                headers=auth(member),
            )
        ).status_code == 200
        assert env.mock.requests[-1]["headers"]["authorization"] == (
            f"Bearer {PLATFORM_KEY}"
        )

        # Deleting again is a 404, not a silent success.
        again = await env.client.delete(
            f"/admin/tenants/{tid}/provider-keys/openai", headers=auth(ADMIN_KEY)
        )
        assert again.status_code == 404


async def test_admin_authz_tenant_scoping(tmp_path):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        acme = await _make_tenant(env.client, "acme")
        globex = await _make_tenant(env.client, "globex")
        # A tenant admin, and a plain member key, inside acme.
        acme_admin = (
            await create_key(
                env.client, "acme-admin", tenant_id=acme["id"], is_admin=True
            )
        )["key"]
        acme_member = (await create_key(env.client, "acme-member", tenant_id=acme["id"]))["key"]

        # A non-admin key cannot touch the vault at all.
        assert (
            await env.client.put(
                f"/admin/tenants/{acme['id']}/provider-keys/openai",
                json={"key": "sk-nope"},
                headers=auth(acme_member),
            )
        ).status_code == 403

        # A tenant admin manages THEIR OWN tenant…
        own = await env.client.put(
            f"/admin/tenants/{acme['id']}/provider-keys/openai",
            json={"key": "sk-acme-managed", "label": "managed by tenant admin"},
            headers=auth(acme_admin),
        )
        assert own.status_code == 200, own.text
        own_list = await env.client.get(
            f"/admin/tenants/{acme['id']}/provider-keys", headers=auth(acme_admin)
        )
        assert own_list.status_code == 200
        assert [e["provider"] for e in own_list.json()["data"]] == ["openai"]

        # …but another tenant's vault is a 404 (anti-leak semantics, the
        # same as cross-tenant key management) — read and write both.
        assert (
            await env.client.get(
                f"/admin/tenants/{globex['id']}/provider-keys",
                headers=auth(acme_admin),
            )
        ).status_code == 404
        assert (
            await env.client.put(
                f"/admin/tenants/{globex['id']}/provider-keys/openai",
                json={"key": "sk-nope"},
                headers=auth(acme_admin),
            )
        ).status_code == 404

        # A nonexistent tenant is a 404, and an invalid provider slug a
        # 400 (it must match the providers: vocabulary's shape).
        assert (
            await env.client.get(
                "/admin/tenants/nonexistent/provider-keys", headers=auth(ADMIN_KEY)
            )
        ).status_code == 404
        assert (
            await env.client.put(
                f"/admin/tenants/{acme['id']}/provider-keys/Not A Provider!",
                json={"key": "sk-nope"},
                headers=auth(ADMIN_KEY),
            )
        ).status_code == 400


# ----------------------------------------------------------------------
# Audit: the hint rides, the key never does
# ----------------------------------------------------------------------


class AuditSink:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})


async def _wait_for(check, timeout: float = 2.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def test_audit_records_carry_hint_never_key(tmp_path):
    sink = AuditSink()
    config = make_config(audit=AuditConfig(webhook_url="https://audit.test/hook"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(sink.handler))
    async for env in _make_env(
        tmp_path, config=config, audit_client=client, vault_master_key=MASTER
    ):
        tenant = await _make_tenant(env.client, "acme")
        stored = "sk-audited-3333"
        assert (
            await _put_provider_key(env.client, tenant["id"], "openai", stored)
        ).status_code == 200
        assert (
            await env.client.delete(
                f"/admin/tenants/{tenant['id']}/provider-keys/openai",
                headers=auth(ADMIN_KEY),
            )
        ).status_code == 200

        await _wait_for(
            lambda: len(
                [p for p in sink.payloads if p["event"].startswith("vault.")]
            )
            >= 2,
            what="both audit deliveries",
        )
        # (The tenant.created record rides the same webhook — filter to
        # the vault events this test is about.)
        vault_payloads = [p for p in sink.payloads if p["event"].startswith("vault.")]
        events = [p["event"] for p in vault_payloads]
        assert events == ["vault.provider_key.upsert", "vault.provider_key.delete"]
        upsert, delete = vault_payloads
        for payload in vault_payloads:
            details = payload["details"]
            assert details["tenant_id"] == tenant["id"]
            assert details["provider"] == "openai"
        # The hint rides the upsert record; the delete record has no
        # hint (the row is gone) — and the key rides NEITHER.
        assert upsert["details"]["key_hint"] == "…3333"
        assert "key_hint" not in delete["details"]
        # The raw key appears in NO delivered record, anywhere.
        assert all(stored not in json.dumps(p) for p in sink.payloads)
