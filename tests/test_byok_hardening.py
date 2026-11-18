"""BYOK hardening — tier 34: vault.strict, the admin Vault tab's
status surface, and Redis-shared cache invalidation.

Three load-bearing properties, against the tier-31 vault:

* **vault.strict** is the platform-economics stance: tenant traffic
  may ONLY ride credentials the tenant stored. The routing ladder for
  a strict tenant is narrowed BEFORE the walk — retries and
  fallbacks can never cross to a provider the tenant did not key —
  and an empty ladder refuses 403 byok_key_required naming the
  providers to key. Platform callers (no tenant), platform admins
  and the platform's own machinery are untouched, the fail-closed
  decrypt story (byok_key_unreadable) is preserved, and both ladder
  heads (streaming and not) plus the realtime connect-time ladder
  narrow identically. Strict with NO master key configured is the
  documented footgun made loud: tenant traffic cannot run at all.
* **GET /admin/vault** reports the honest state (enabled or the
  reason it is not, strict, TTL, invalidation backend) plus the
  caller's tenant context, so the dashboard's Vault tab works for
  platform admins (any tenant) and tenant admins (pinned to their
  own — the only one the provider-key endpoints let them touch).
  No secrets anywhere in the payload.
* **vault.backend: redis** closes the multi-replica window: admin
  writes publish (tenant, provider) invalidations on a Redis channel
  and every replica's listener drops its cached entry — and its
  NEGATIVE twin, the "no key stored" answer that would otherwise
  keep serving platform credentials for up to ttl_seconds after
  another replica stored a key. Best-effort by design: a failed
  publish or a malformed message never fails the admin write, and
  the TTL stays the backstop.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis

from gateway.config import (
    DeploymentConfig,
    ModelConfig,
    ProviderConfig,
    Settings,
    VaultConfig,
)
from gateway.core.redis_backends import RedisVaultInvalidator
from gateway.core.vault import VaultEntry, VaultService
from gateway.db import dispose_engine, init_engine, reset_engine_for_tests
from gateway.main import create_app

from .conftest import (
    ADMIN_KEY,
    MockUpstream,
    _make_env,
    auth,
    chat_body,
    create_key,
    make_config,
)
from .test_realtime import WSClient as RTWSClient, _rejected as rt_rejected, _rt_config

# One master key pair per module run (Fernet keys are random; what
# matters is that a DIFFERENT key cannot decrypt the first one's rows).
MASTER = Fernet.generate_key().decode()
MASTER2 = Fernet.generate_key().decode()
PLATFORM_KEY = "provider-key"  # make_config's default openai key


def _strict(**vault_fields) -> VaultConfig:
    return VaultConfig(strict=True, **vault_fields)


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
    """Upstream Authorization headers seen by the mock (optionally
    only for requests whose body model matches)."""
    out = []
    for req in env.mock.requests:
        body = req.get("body") or {}
        if model is not None and body.get("model") != model:
            continue
        out.append(req["headers"].get("authorization", ""))
    return out


def _dual_config(strict_on: bool):
    """A model with an azure (preferred) and an openai (fallback)
    deployment — the ladder strict must be able to narrow. Azure
    builds deployment-addressed paths itself and authenticates with
    the api-key header, like the tier-31 failover fixture."""
    return make_config(
        vault=_strict() if strict_on else VaultConfig(),
        providers={
            "openai": ProviderConfig(
                api_base="http://mockupstream.test/v1", api_key="platform-openai"
            ),
            "azure": ProviderConfig(
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
                        name="azure/leg", provider="azure", model="model-b"
                    ),
                    DeploymentConfig(
                        name="openai/leg",
                        provider="openai",
                        model="model-a",
                        priority=200,
                    ),
                ],
            ),
        ],
        pricing={"model-a": {"input": 2.0}, "model-b": {"input": 2.0}},
    )


# ----------------------------------------------------------------------
# vault.strict: the routing stance
# ----------------------------------------------------------------------


async def test_strict_refuses_unkeyed_tenant(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(vault=_strict()), vault_master_key=MASTER
    ):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(member),
        )
        assert resp.status_code == 403, resp.text
        error = resp.json()["error"]
        assert error["code"] == "byok_key_required"
        # The refusal says exactly what to key: the provider, the
        # model, and the admin path that fixes it.
        assert "openai" in error["message"]
        assert "test-model" in error["message"]
        assert "provider-keys" in error["message"]
        # Refused BEFORE dispatch: nothing reached any provider…
        assert env.mock.requests == []
        # …and the refusal is observable like every failure.
        assert (await _last_log(env.client))["status"] == "error"


async def test_strict_off_rides_platform_key(tmp_path):
    """The baseline: without strict, a keyless tenant rides the
    platform's configured key — byte-for-byte tier-31 behavior."""
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(member),
        )
        assert resp.status_code == 200, resp.text
        assert _auths(env) == [f"Bearer {PLATFORM_KEY}"]


async def test_strict_keyed_tenant_rides_their_key(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(vault=_strict()), vault_master_key=MASTER
    ):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        stored = "sk-acme-own-key-42"
        await _put_provider_key(env.client, tenant["id"], "openai", stored)

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(member),
        )
        assert resp.status_code == 200, resp.text
        assert _auths(env) == [f"Bearer {stored}"]
        assert resp.headers["x-gateway-byok-provider"] == "openai"


async def test_strict_leaves_platform_callers_alone(tmp_path):
    """Strict is a TENANT stance: platform admins and no-tenant keys
    ride the platform's configured keys, exactly as before."""
    async for env in _make_env(
        tmp_path, config=make_config(vault=_strict()), vault_master_key=MASTER
    ):
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="admin"),
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200, resp.text

        platform_user = (await create_key(env.client, "platform-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="user"),
            headers=auth(platform_user),
        )
        assert resp.status_code == 200, resp.text
        assert _auths(env) == [f"Bearer {PLATFORM_KEY}"] * 2


async def test_strict_narrows_the_ladder_hot_reload_contrast(tmp_path):
    """The core strict semantics in one story, on a dual ladder:

    * without strict, the keyless tenant is served by the PREFERRED
      azure leg with the PLATFORM's azure key (override semantics,
      per provider);
    * after storing an openai key and hot-enabling strict, the azure
      leg is REMOVED from the tenant's ladder — the request serves
      from the keyed openai fallback alone, never touching azure.

    Also pins that the vault section reports restart_required on
    reload (its backend/TTL are service-constructor knobs) while the
    strict stance itself applies live — it is read off router.config
    per request.
    """
    async for env in _make_env(
        tmp_path, config=_dual_config(strict_on=False), vault_master_key=MASTER
    ):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]

        # Leg 1 — strict off: the preferred azure leg serves, on the
        # platform's credential.
        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello", model="dual"),
            headers=auth(member),
        )
        assert first.status_code == 200, first.text
        assert first.headers["x-gateway-deployment"] == "azure/leg"
        azure_legs = [
            req["headers"].get("api-key", "")
            for req in env.mock.requests
            if (req.get("body") or {}).get("model") == "model-b"
        ]
        assert azure_legs == ["platform-azure"]

        # The tenant keys openai; strict goes on via the reload path.
        await _put_provider_key(env.client, tenant["id"], "openai", "sk-acme-openai")
        new_config = _dual_config(strict_on=True)
        summary = env.app.state.pipeline.apply_config(new_config)
        env.app.state.config = new_config
        assert summary["restart_required"] == ["vault"]

        # Leg 2 — strict on: azure is unkeyed, so the narrowed ladder
        # is [openai/leg] and the tenant's key rides it. (Distinct
        # body from leg 1: the response cache is live in this config,
        # and a byte-identical replay would never re-route.)
        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello again", model="dual"),
            headers=auth(member),
        )
        assert second.status_code == 200, second.text
        assert second.headers["x-gateway-deployment"] == "openai/leg"
        assert _auths(env, model="model-a") == ["Bearer sk-acme-openai"]
        # Azure was never consulted this time — one model-b request
        # in total (leg 1's), not two.
        azure_legs = [
            req["headers"].get("api-key", "")
            for req in env.mock.requests
            if (req.get("body") or {}).get("model") == "model-b"
        ]
        assert azure_legs == ["platform-azure"]


async def test_strict_refusal_names_every_provider(tmp_path):
    async for env in _make_env(
        tmp_path, config=_dual_config(strict_on=True), vault_master_key=MASTER
    ):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello", model="dual"),
            headers=auth(member),
        )
        assert resp.status_code == 403
        error = resp.json()["error"]
        assert error["code"] == "byok_key_required"
        # Every provider the model runs on, sorted — the operator's
        # checklist for what the tenant must key.
        assert "(its deployments run on: azure, openai)" in error["message"]
        assert env.mock.requests == []


async def test_strict_without_master_key_fails_tenant_traffic(tmp_path):
    """The documented footgun, made loud: strict + no vault master
    key means no tenant can key ANY provider — so tenant traffic
    refuses rather than silently spending the platform's keys."""
    async for env in _make_env(tmp_path, config=make_config(vault=_strict())):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(member),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "byok_key_required"
        # Platform traffic still runs (the platform's own keys).
        assert (
            await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="hello"),
                headers=auth(ADMIN_KEY),
            )
        ).status_code == 200


async def test_strict_keeps_unreadable_entries_fail_closed(tmp_path):
    """A strict ladder still propagates the decrypt failure: an entry
    that exists but cannot be read (a rotated-away master key) is
    byok_key_unreadable — never a silent platform ride."""
    async for env in _make_env(
        tmp_path, config=make_config(vault=_strict()), vault_master_key=MASTER
    ):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        await _put_provider_key(env.client, tenant["id"], "openai", "sk-acme-key")

        # What a master-key rotation restart looks like: a service
        # built on the new key replaces the old one everywhere it is
        # referenced (admin plane + serving plane).
        rotated = VaultService(MASTER2, ttl_seconds=30)
        env.app.state.vault = rotated
        env.app.state.pipeline.router.vault = rotated

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(member),
        )
        assert resp.status_code == 500, resp.text
        assert resp.json()["error"]["code"] == "byok_key_unreadable"
        assert env.mock.requests == []


async def test_strict_refuses_streaming_too(tmp_path):
    """The streaming ladder head narrows identically: the refusal is
    the plain error envelope, before any SSE is committed."""
    async for env in _make_env(
        tmp_path, config=make_config(vault=_strict()), vault_master_key=MASTER
    ):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello", stream=True),
            headers=auth(member),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "byok_key_required"
        assert env.mock.requests == []


async def test_strict_refuses_realtime_session_open(tmp_path):
    """The realtime connect-time ladder narrows identically — and the
    refusal is delivered the realtime way: an in-band OpenAI-shaped
    error event followed by close 1008, upstream never contacted
    (the dead-port realtime_base proves nothing was even dialed)."""
    config = _rt_config(None, vault=_strict())
    async for env in _make_env(tmp_path, config=config, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        ws = RTWSClient(env.app, "/v1/realtime?model=test-model", key=member)
        await ws.connect()
        await rt_rejected(ws, code="byok_key_required")


# ----------------------------------------------------------------------
# Redis-shared cache invalidation (vault.backend: redis)
# ----------------------------------------------------------------------


async def test_admin_writes_publish_invalidation(tmp_path):
    """PUT and DELETE publish (tenant, provider) on the channel —
    observed here by a raw subscriber standing in for the fleet."""
    redis = FakeAsyncRedis()
    async for env in _make_env(
        tmp_path,
        config=make_config(vault=VaultConfig(backend="redis")),
        redis_client=redis,
        vault_master_key=MASTER,
    ):
        channel = f"{env.app.state.config.redis.prefix}:vault-invalidation"
        assert env.app.state.vault_invalidator is not None

        tenant = await _make_tenant(env.client, "acme")
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)

        put = await _put_provider_key(env.client, tenant["id"], "openai", "sk-x")
        assert put.status_code == 200, put.text
        await env.client.delete(
            f"/admin/tenants/{tenant['id']}/provider-keys/openai",
            headers=auth(ADMIN_KEY),
        )

        payloads = []
        while True:
            message = await pubsub.get_message(timeout=0.5)
            if message is None:
                break
            if message.get("type") == "message":
                payloads.append(json.loads(message["data"]))
        assert payloads == [
            {"tenant_id": tenant["id"], "provider": "openai"},
            {"tenant_id": tenant["id"], "provider": "openai"},
        ]
        await pubsub.aclose()


async def test_two_replicas_converge_immediately(tmp_path):
    """The multi-replica window, closed: replica B answers "no key
    stored" from its NEGATIVE cache; replica A stores the tenant's
    key; A's publish drops B's cached answer and B serves the very
    next request on the TENANT's key — where the memory backend
    would keep riding the platform's for up to ttl_seconds. The
    delete direction converges the same way.
    """
    redis = FakeAsyncRedis()
    reset_engine_for_tests()
    db_url = f"sqlite+aiosqlite:///{tmp_path}/replicas.db"
    await init_engine(db_url)

    def build() -> tuple:
        mock = MockUpstream()
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
        app = create_app(
            settings=Settings(
                admin_key=ADMIN_KEY,
                database_url=db_url,
                vault_master_key=MASTER,
            ),
            config=make_config(
                vault=VaultConfig(backend="redis", ttl_seconds=60)
            ),
            http_client=http_client,
            redis_client=redis,
        )
        app.state.admin_key = ADMIN_KEY  # ASGITransport does not run the lifespan
        return app, mock, http_client

    app_a, mock_a, http_a = build()
    app_b, mock_b, http_b = build()
    # Replica B's listener — the lifespan loop pytest never starts,
    # so this test runs the very coroutine production would.
    listener_b = asyncio.create_task(app_b.state.vault_invalidator.listen())
    try:
        client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="http://a.test"
        )
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://b.test"
        )
        tenant = await _make_tenant(client_a, "acme")
        member = (await create_key(client_a, "member", tenant_id=tenant["id"]))["key"]
        pair = (tenant["id"], "openai")

        # B serves the member with the platform key and caches the
        # absence — the stale window the invalidation must close.
        # (Each request below carries a DISTINCT body: the response
        # cache is live in this config and a byte-identical replay
        # would never re-route through the vault.)
        first = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="probe one"),
            headers=auth(member),
        )
        assert first.status_code == 200, first.text
        assert mock_b.requests[-1]["headers"]["authorization"] == f"Bearer {PLATFORM_KEY}"
        assert pair in app_b.state.vault._absent

        # Replica A stores the credential (and publishes).
        put = await _put_provider_key(client_a, tenant["id"], "openai", "sk-acme-live")
        assert put.status_code == 200, put.text

        # B converges as its listener applies the message — the
        # negative-cache entry is gone without waiting out the TTL.
        for _ in range(100):
            if pair not in app_b.state.vault._absent:
                break
            await asyncio.sleep(0.02)
        assert pair not in app_b.state.vault._absent

        second = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="probe two"),
            headers=auth(member),
        )
        assert second.status_code == 200, second.text
        assert mock_b.requests[-1]["headers"]["authorization"] == "Bearer sk-acme-live"
        assert second.headers["x-gateway-byok-provider"] == "openai"
        assert pair in app_b.state.vault._cache  # warm now, not absent

        # The delete direction converges too: A deletes, B drops the
        # WARM entry, and the platform key serves again.
        await client_a.delete(
            f"/admin/tenants/{tenant['id']}/provider-keys/openai",
            headers=auth(ADMIN_KEY),
        )
        for _ in range(100):
            if pair not in app_b.state.vault._cache:
                break
            await asyncio.sleep(0.02)
        third = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="probe three"),
            headers=auth(member),
        )
        assert third.status_code == 200, third.text
        assert mock_b.requests[-1]["headers"]["authorization"] == f"Bearer {PLATFORM_KEY}"
        await client_a.aclose()
        await client_b.aclose()
    finally:
        listener_b.cancel()
        try:
            await listener_b
        except asyncio.CancelledError:
            pass
        await http_a.aclose()
        await http_b.aclose()
        await dispose_engine()


async def test_memory_backend_wires_nothing(tmp_path):
    """The default backend keeps the tier-31 shape exactly: no
    invalidator object, no listener task — and admin writes still
    work (the None callback path is a no-op)."""
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        assert env.app.state.vault_invalidator is None
        tenant = await _make_tenant(env.client, "acme")
        put = await _put_provider_key(env.client, tenant["id"], "openai", "sk-1")
        assert put.status_code == 200, put.text
        assert put.json()["key_hint"].startswith("…")


def test_listener_skips_malformed_payloads():
    """A foreign or corrupt message on the channel must neither wedge
    the loop nor drop a live cache entry; a well-formed one drops
    exactly the entry it names."""
    service = VaultService(MASTER, ttl_seconds=30)
    entry = VaultEntry(
        tenant_id="t1", provider="openai", key="k", label=None, hint="…1234"
    )
    service._cache[("t1", "openai")] = (
        entry,
        datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    invalidator = RedisVaultInvalidator(client=None, channel="ch")
    invalidator.bind(service)

    invalidator._apply(b"not json at all")
    invalidator._apply(json.dumps({"tenant_id": "t1"}))
    invalidator._apply(["not", "an", "object"])
    assert ("t1", "openai") in service._cache  # survived the garbage

    invalidator._apply(json.dumps({"tenant_id": "t1", "provider": "openai"}))
    assert ("t1", "openai") not in service._cache  # named entry dropped


async def test_publish_failure_is_swallowed():
    """A down Redis must never fail the admin write: the publish is
    best-effort, the local drop already happened, the TTL is the
    documented backstop."""

    class _Boom:
        async def publish(self, *args, **kwargs):
            raise RuntimeError("redis is down")

    invalidator = RedisVaultInvalidator(client=_Boom(), channel="ch")
    await invalidator.publish("t1", "openai")  # must not raise


# ----------------------------------------------------------------------
# GET /admin/vault: the status surface the dashboard's Vault tab
# ----------------------------------------------------------------------


async def test_vault_status_reports_shape(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(vault=VaultConfig(strict=True, ttl_seconds=45)),
        vault_master_key=MASTER,
    ):
        resp = await env.client.get("/admin/vault", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "enabled": True,
            "unavailable_reason": None,
            "strict": True,
            "ttl_seconds": 45,
            "invalidation_backend": "memory",
            "admin_context": None,
        }


async def test_vault_status_pins_tenant_admins(tmp_path):
    """A tenant admin's status carries their own tenant — the Vault
    tab pins them to it (the only one the provider-key endpoints let
    them touch); a platform admin gets null and picks from the list."""
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        tenant = await _make_tenant(env.client, "acme")
        tadmin = (
            await create_key(env.client, "tadmin", is_admin=True, tenant_id=tenant["id"])
        )["key"]
        resp = await env.client.get("/admin/vault", headers=auth(tadmin))
        assert resp.status_code == 200, resp.text
        assert resp.json()["admin_context"] == {
            "tenant_id": tenant["id"],
            "tenant_name": "acme",
        }
        # The strict stance is live in the status (config read per
        # call — a hot reload shows up on the next poll).
        new_config = make_config(vault=_strict())
        env.app.state.pipeline.apply_config(new_config)
        env.app.state.config = new_config
        resp = await env.client.get("/admin/vault", headers=auth(tadmin))
        assert resp.json()["strict"] is True


async def test_vault_status_disabled_reason_and_403(tmp_path):
    """No master key: the status says so honestly (naming the env
    var to set) instead of pretending; and only admins may ask."""
    async for env in _make_env(tmp_path):
        resp = await env.client.get("/admin/vault", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert "GATEWAY_VAULT_MASTER_KEY" in data["unavailable_reason"]

        # Data-plane keys are not admins on this surface.
        plain = (await create_key(env.client, "plain"))["key"]
        assert (
            await env.client.get("/admin/vault", headers=auth(plain))
        ).status_code == 403
