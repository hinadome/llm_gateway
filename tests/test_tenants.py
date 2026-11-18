"""Tests for multi-tenancy: tenant CRUD, admin scoping, policy
enforcement, cache isolation and pooled spend/alerts."""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import httpx
import pytest

from gateway.config import AlertsConfig, DeploymentConfig, ModelConfig

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

# Pricing that makes one non-stream mock request (10 prompt tokens) cost
# $20: 10 tokens x $2/token ($2M per million). Copied from test_alerts.
BIG_PRICING = {"model-a": {"input": 2_000_000, "output": 0}}

EMB_MODEL = ModelConfig(
    name="text-embedding-mock",
    deployments=[
        DeploymentConfig(
            name="openai/emb-primary", provider="openai", model="embed-mock-a"
        )
    ],
)
EMB_PRICING = {"text-embedding-mock": {"input": 0.02, "output": 0.0}}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _create_tenant(client: httpx.AsyncClient, name: str, **fields) -> dict:
    resp = await client.post(
        "/admin/tenants", json={"name": name, **fields}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _tenant_row(client: httpx.AsyncClient, tenant_id: str) -> dict:
    resp = await client.get(f"/admin/tenants/{tenant_id}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _key_row(client: httpx.AsyncClient, key_id: str) -> dict:
    resp = await client.get(f"/admin/keys/{key_id}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_key_as(
    client: httpx.AsyncClient, admin_key: str, name: str, **fields
) -> httpx.Response:
    """POST /admin/keys as an arbitrary admin (the conftest create_key
    always authenticates as the platform admin key)."""
    return await client.post(
        "/admin/keys", json={"name": name, **fields}, headers=auth(admin_key)
    )


class WebhookSink:
    """Captures webhook deliveries (mirrors test_alerts)."""

    def __init__(self, status: int = 200) -> None:
        self.payloads: list[dict] = []
        self.attempts = 0
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        self.payloads.append(json.loads(request.content))
        return httpx.Response(self.status, json={"ok": True})


async def _wait_for(check, timeout: float = 2.0, what: str = "condition"):
    """Poll a sync or async predicate until truthy or the deadline hits.

    Spend accounting, webhook delivery and request logging all run in
    the pipeline's background tasks, so e2e tests must poll for their
    side effects.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _make_alert_env(tmp_path, alerts: AlertsConfig, sink: WebhookSink):
    config = make_config(pricing=BIG_PRICING, alerts=alerts)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for ns in _make_env(tmp_path, config=config, alert_client=ac):
            ns.webhooks = sink
            yield ns


async def _seed_multi_tenant_traffic(tmp_path):
    """Env with two tenants plus a platform key, one request each, all
    log rows flushed — the base for logs/stats/usage scoping tests."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        acme_key = await create_key(env.client, "acme-member", tenant_id=acme["id"])
        beta_key = await create_key(env.client, "beta-member", tenant_id=beta["id"])
        platform_key = await create_key(env.client, "platform-user")

        for k, content in (
            (acme_key, "from acme"),
            (beta_key, "from beta"),
            (platform_key, "from platform"),
        ):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content=content),
                headers=auth(k["key"]),
            )
            assert resp.status_code == 200

        async def _logs_total_ge(n: int) -> bool:
            r = await env.client.get("/admin/logs?limit=10", headers=auth(ADMIN_KEY))
            return r.json()["total"] >= n

        await _wait_for(lambda: _logs_total_ge(3), what="all log rows flushed")
        yield env, acme, beta, admin, acme_key, beta_key, platform_key


# ----------------------------------------------------------------------
# Tenant CRUD (platform admins)
# ----------------------------------------------------------------------


async def test_tenant_crud_roundtrip(tmp_path):
    async for env in _make_env(tmp_path):
        created = await _create_tenant(
            env.client,
            "acme",
            display_name="Acme Corp",
            allowed_models=["test-*"],
            rpm_limit=100,
            tpm_limit=50_000,
            max_budget_usd=250.0,
            daily_budget_usd=10.0,
            monthly_budget_usd=100.0,
        )
        assert created["name"] == "acme"
        assert created["display_name"] == "Acme Corp"
        assert created["is_active"] is True
        assert created["allowed_models"] == ["test-*"]
        assert created["rpm_limit"] == 100
        assert created["tpm_limit"] == 50_000
        assert created["max_budget_usd"] == 250.0
        assert created["daily_budget_usd"] == 10.0
        assert created["monthly_budget_usd"] == 100.0
        assert created["spend_usd"] == 0.0
        assert created["budget_alerts_fired"] is None

        listed = (
            await env.client.get("/admin/tenants", headers=auth(ADMIN_KEY))
        ).json()["data"]
        assert [t["name"] for t in listed] == ["acme"]

        # patch: display name + limits; other fields untouched
        resp = await env.client.patch(
            f"/admin/tenants/{created['id']}",
            json={"display_name": "ACME", "rpm_limit": 200},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200
        patched = resp.json()
        assert patched["display_name"] == "ACME"
        assert patched["rpm_limit"] == 200
        assert patched["max_budget_usd"] == 250.0

        # the slug is immutable — it is simply not a patchable field
        row = await _tenant_row(env.client, created["id"])
        assert row["name"] == "acme"

        # delete with no keys referencing the tenant
        resp = await env.client.delete(
            f"/admin/tenants/{created['id']}", headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200
        assert resp.json() == {"id": created["id"], "deleted": True}
        resp = await env.client.get(
            f"/admin/tenants/{created['id']}", headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 404


@pytest.mark.parametrize("bad", ["has space", "-acme", "acmé", "", "a" * 65, "ac me!"])
async def test_tenant_slug_validation(tmp_path, bad):
    # The slug surfaces in API responses, log filters and the
    # X-Gateway-Tenant header — header-hostile names are rejected
    # (the gateway maps request-validation errors to its 400 shape).
    async for env in _make_env(tmp_path):
        resp = await env.client.post(
            "/admin/tenants", json={"name": bad}, headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 400, resp.text


async def test_tenant_duplicate_name_409(tmp_path):
    async for env in _make_env(tmp_path):
        await _create_tenant(env.client, "acme")
        resp = await env.client.post(
            "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 409


async def test_tenant_delete_refused_while_keys_exist(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        # deleting with member keys would orphan them (a dangling
        # tenant_id fails closed at auth time) — refused with guidance
        resp = await env.client.delete(
            f"/admin/tenants/{acme['id']}", headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 409
        assert "re-home or revoke" in resp.json()["error"]["message"]

        # re-home the key to the platform, then the delete succeeds
        resp = await env.client.patch(
            f"/admin/keys/{key['id']}", json={"tenant_id": None}, headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] is None

        resp = await env.client.delete(
            f"/admin/tenants/{acme['id']}", headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200


async def test_tenant_budget_patch_rearm(tmp_path):
    sink = WebhookSink()
    alerts = AlertsConfig(webhook_url="http://alerts.test/hook", max_attempts=1)
    async for env in _make_alert_env(tmp_path, alerts, sink):
        acme = await _create_tenant(env.client, "acme", max_budget_usd=30.0)
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        # one request: $20 of $30 (66.7%) crosses the 50% level
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="one"),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 200
        await _wait_for(lambda: len(sink.payloads) >= 1, what="tenant 50% alert")
        row = await _tenant_row(env.client, acme["id"])
        assert row["budget_alerts_fired"] == [50]

        # raising the budget re-arms: $20 of $60 is 33% -> nothing crossed
        resp = await env.client.patch(
            f"/admin/tenants/{acme['id']}",
            json={"max_budget_usd": 60.0},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["budget_alerts_fired"] == []


# ----------------------------------------------------------------------
# Tenant admins: scoped key management
# ----------------------------------------------------------------------


async def test_tenant_admin_key_reflected(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        admin_key = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        assert admin_key["is_admin"] is True
        assert admin_key["tenant_id"] == acme["id"]

        member = await create_key(env.client, "acme-member", tenant_id=acme["id"])
        assert member["is_admin"] is False
        assert member["tenant_id"] == acme["id"]

        # platform admin keys stay tenant-less
        ops = await create_key(env.client, "ops", is_admin=True)
        assert ops["is_admin"] is True
        assert ops["tenant_id"] is None


async def test_tenant_admin_scoped_key_access(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        acme_admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        await create_key(env.client, "acme-member", tenant_id=acme["id"])
        beta_member = await create_key(env.client, "beta-member", tenant_id=beta["id"])

        # list: own tenant only
        resp = await env.client.get("/admin/keys", headers=auth(acme_admin["key"]))
        assert resp.status_code == 200
        names = {k["name"] for k in resp.json()["data"]}
        assert names == {"acme-admin", "acme-member"}

        # cross-tenant detail/mutate is a 404, not a 403 — confirming
        # another tenant's key id exists would leak it
        resp = await env.client.get(
            f"/admin/keys/{beta_member['id']}", headers=auth(acme_admin["key"])
        )
        assert resp.status_code == 404
        resp = await env.client.patch(
            f"/admin/keys/{beta_member['id']}",
            json={"rpm_limit": 5},
            headers=auth(acme_admin["key"]),
        )
        assert resp.status_code == 404
        resp = await env.client.delete(
            f"/admin/keys/{beta_member['id']}", headers=auth(acme_admin["key"])
        )
        assert resp.status_code == 404

        # the beta key was untouched
        row = await _key_row(env.client, beta_member["id"])
        assert row["rpm_limit"] is None
        assert row["is_active"] is True

        # own-tenant keys are fully manageable
        member = (await create_key(env.client, "acme-second", tenant_id=acme["id"]))
        resp = await env.client.get(
            f"/admin/keys/{member['id']}", headers=auth(acme_admin["key"])
        )
        assert resp.status_code == 200
        resp = await env.client.patch(
            f"/admin/keys/{member['id']}",
            json={"rpm_limit": 5},
            headers=auth(acme_admin["key"]),
        )
        assert resp.status_code == 200
        assert resp.json()["rpm_limit"] == 5


async def test_tenant_admin_key_creation_forced(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        acme_admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )

        # omitted tenant_id -> forced into the admin's own tenant
        resp = await _create_key_as(env.client, acme_admin["key"], "forced")
        assert resp.status_code == 201
        assert resp.json()["tenant_id"] == acme["id"]

        # explicit own tenant is fine
        resp = await _create_key_as(
            env.client, acme_admin["key"], "explicit", tenant_id=acme["id"]
        )
        assert resp.status_code == 201
        assert resp.json()["tenant_id"] == acme["id"]

        # someone else's tenant is refused, not silently overridden
        resp = await _create_key_as(
            env.client, acme_admin["key"], "sneaky", tenant_id=beta["id"]
        )
        assert resp.status_code == 400
        assert "own tenant" in resp.json()["error"]["message"]

        # platform admins place keys in any existing tenant...
        resp = await _create_key_as(env.client, ADMIN_KEY, "placed", tenant_id=beta["id"])
        assert resp.status_code == 201
        assert resp.json()["tenant_id"] == beta["id"]
        # ...but not in one that does not exist
        resp = await _create_key_as(env.client, ADMIN_KEY, "nowhere", tenant_id="missing")
        assert resp.status_code == 400


async def test_tenant_admin_cannot_rehome(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        acme_admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        member = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        # moving to another tenant or out to the platform: both refused
        for target in (beta["id"], None):
            resp = await env.client.patch(
                f"/admin/keys/{member['id']}",
                json={"tenant_id": target},
                headers=auth(acme_admin["key"]),
            )
            assert resp.status_code == 403
            assert "cannot move keys" in resp.json()["error"]["message"]

        # the key stayed put
        row = await _key_row(env.client, member["id"])
        assert row["tenant_id"] == acme["id"]

        # platform admins re-home, with the target validated
        resp = await env.client.patch(
            f"/admin/keys/{member['id']}",
            json={"tenant_id": beta["id"]},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == beta["id"]

        resp = await env.client.patch(
            f"/admin/keys/{member['id']}",
            json={"tenant_id": "missing"},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 400


async def test_tenant_admin_platform_endpoints_403(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        member = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        checks = [
            ("GET", "/admin/tenants", None),
            ("POST", "/admin/tenants", {"name": "sneaky"}),
            ("GET", f"/admin/tenants/{acme['id']}", None),
            ("PATCH", f"/admin/tenants/{acme['id']}", {"rpm_limit": 1}),
            ("DELETE", f"/admin/tenants/{acme['id']}", None),
            ("GET", "/admin/prompts", None),
            ("POST", "/admin/prompts", {"name": "p", "body": "b"}),
            ("GET", "/admin/routing", None),
            ("GET", "/admin/guardrails", None),
            ("GET", "/admin/runaway", None),
            ("POST", f"/admin/runaway/{admin['id']}/release", None),
            ("GET", "/admin/concurrency", None),
            ("POST", "/admin/reload", None),
            ("GET", "/admin/backups", None),
            ("POST", "/admin/backup", None),
            ("DELETE", "/admin/backups/nope", None),
        ]
        for method, path, body in checks:
            resp = await env.client.request(
                method, path, json=body, headers=auth(admin["key"])
            )
            assert resp.status_code == 403, (method, path, resp.status_code)

        # the same endpoints work for the platform admin
        resp = await env.client.get("/admin/tenants", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200

        # and a plain member key never sees the admin API at all
        resp = await env.client.get("/admin/keys", headers=auth(member["key"]))
        assert resp.status_code == 403


async def test_tenant_admin_logs_and_usage_scoped(tmp_path):
    async for env, acme, beta, admin, acme_key, beta_key, platform_key in (
        _seed_multi_tenant_traffic(tmp_path)
    ):
        # the tenant admin's log list is forced to their tenant
        resp = await env.client.get("/admin/logs", headers=auth(admin["key"]))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert {r["key_name"] for r in data} == {"acme-member"}
        assert all(r["tenant_id"] == acme["id"] for r in data)

        # a ?tenant_id= parameter cannot escape the forced scope
        resp = await env.client.get(
            f"/admin/logs?tenant_id={beta['id']}", headers=auth(admin["key"])
        )
        assert {r["key_name"] for r in resp.json()["data"]} == {"acme-member"}

        # cross-tenant log detail is a 404, same as an unknown id
        rows = (
            await env.client.get("/admin/logs?limit=10", headers=auth(ADMIN_KEY))
        ).json()["data"]
        beta_log_id = next(r["id"] for r in rows if r["key_name"] == "beta-member")
        acme_log_id = next(r["id"] for r in rows if r["key_name"] == "acme-member")

        resp = await env.client.get(
            f"/admin/logs/{beta_log_id}", headers=auth(admin["key"])
        )
        assert resp.status_code == 404
        resp = await env.client.get(
            f"/admin/logs/{acme_log_id}", headers=auth(admin["key"])
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == acme["id"]

        # usage export: forced scoping, the forced tenant reported
        resp = await env.client.get("/admin/usage/export", headers=auth(admin["key"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["filters"]["tenant_id"] == acme["id"]
        assert {r["key_name"] for r in body["requests"]} == {"acme-member"}
        assert all(r["tenant_id"] == acme["id"] for r in body["requests"])
        assert "by_tenant" not in body["summary"]


async def test_platform_admin_tenant_log_filter(tmp_path):
    async for env, acme, beta, admin, acme_key, beta_key, platform_key in (
        _seed_multi_tenant_traffic(tmp_path)
    ):
        # every row carries the calling key's tenant (None = platform)
        rows = (
            await env.client.get("/admin/logs?limit=10", headers=auth(ADMIN_KEY))
        ).json()["data"]
        by_key = {r["key_name"]: r for r in rows}
        assert by_key["acme-member"]["tenant_id"] == acme["id"]
        assert by_key["beta-member"]["tenant_id"] == beta["id"]
        assert by_key["platform-user"]["tenant_id"] is None

        # opt-in tenant filter
        resp = await env.client.get(
            f"/admin/logs?tenant_id={acme['id']}", headers=auth(ADMIN_KEY)
        )
        assert {r["key_name"] for r in resp.json()["data"]} == {"acme-member"}
        resp = await env.client.get(
            "/admin/logs?tenant_id=missing", headers=auth(ADMIN_KEY)
        )
        assert resp.json()["total"] == 0

        # platform usage export: per-tenant chargeback breakdown
        resp = await env.client.get("/admin/usage/export", headers=auth(ADMIN_KEY))
        body = resp.json()
        by_tenant = {e["tenant_id"]: e for e in body["summary"]["by_tenant"]}
        assert by_tenant[acme["id"]]["requests"] == 1
        assert by_tenant[None]["requests"] == 1


# ----------------------------------------------------------------------
# Tenant policy enforcement
# ----------------------------------------------------------------------


async def test_tenant_allowlist_narrows_member_keys(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme", allowed_models=["test-*"])
        member = await create_key(env.client, "acme-member", tenant_id=acme["id"])
        tenant_admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        platform_admin = await create_key(env.client, "ops", is_admin=True)

        # member key: allowed model fine...
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="test-model"),
            headers=auth(member["key"]),
        )
        assert resp.status_code == 200
        # ...model outside the tenant: 404
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model"),
            headers=auth(member["key"]),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"

        # tenant admins get no bypass: the ceiling binds every member
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model"),
            headers=auth(tenant_admin["key"]),
        )
        assert resp.status_code == 404

        # platform admins are outside every tenant
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model"),
            headers=auth(platform_admin["key"]),
        )
        assert resp.status_code == 200


async def test_key_and_tenant_allowlists_intersect(tmp_path):
    # A key allowing only other-model inside a tenant allowing only
    # test-model: no model satisfies both layers.
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme", allowed_models=["test-model"])
        key = await create_key(
            env.client, "picky", allowed_models=["other-model"], tenant_id=acme["id"]
        )

        for model in ("test-model", "other-model"):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(model=model),
                headers=auth(key["key"]),
            )
            assert resp.status_code == 404, model


async def test_tenant_rate_limit_pools_keys(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme", rpm_limit=2)
        key_a = await create_key(env.client, "acme-a", tenant_id=acme["id"])
        key_b = await create_key(env.client, "acme-b", tenant_id=acme["id"])

        for content in ("one", "two"):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content=content),
                headers=auth(key_a["key"]),
            )
            assert resp.status_code == 200

        # third request under a DIFFERENT key: the tenant window is
        # exhausted — the two member keys share it
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="three"),
            headers=auth(key_b["key"]),
        )
        assert resp.status_code == 429
        err = resp.json()["error"]
        assert err["code"] == "rate_limit_exceeded"
        assert "tenant 'acme'" in err["message"]
        assert "Retry-After" in resp.headers


async def test_tenant_budget_exceeded_429(tmp_path):
    # One request costs $20 (BIG_PRICING); the tenant caps total spend
    # at $20, so the second request fails closed.
    config = make_config(pricing=BIG_PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "budgeted", max_budget_usd=20.0)
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="first"),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 200

        async def _spent() -> float:
            return (await _tenant_row(env.client, acme["id"]))["spend_usd"]

        async def _spent_enough() -> bool:
            return (await _spent()) >= 20.0

        await _wait_for(_spent_enough, what="tenant spend recorded")

        # the next request fails: the auth-time snapshot sees spent budget
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="second"),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 429
        err = resp.json()["error"]
        assert err["code"] == "insufficient_quota"
        assert "Tenant 'budgeted' has exceeded its budget" in err["message"]

        # the key itself is fine: only the tenant's ceiling was hit
        row = await _key_row(env.client, key["id"])
        assert row["spend_usd"] == 20.0


async def test_inactive_tenant_kill_switch(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        member = await create_key(env.client, "acme-member", tenant_id=acme["id"])
        admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )

        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(member["key"])
        )
        assert resp.status_code == 200

        # disabling the tenant kills every member key, admins included
        resp = await env.client.patch(
            f"/admin/tenants/{acme['id']}",
            json={"is_active": False},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="again"),
            headers=auth(member["key"]),
        )
        assert resp.status_code == 401
        resp = await env.client.get("/admin/keys", headers=auth(admin["key"]))
        assert resp.status_code == 401

        # re-enabling restores service — the keys were never revoked
        await env.client.patch(
            f"/admin/tenants/{acme['id']}",
            json={"is_active": True},
            headers=auth(ADMIN_KEY),
        )
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="once more"),
            headers=auth(member["key"]),
        )
        assert resp.status_code == 200


async def test_x_gateway_tenant_header(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        member = await create_key(env.client, "acme-member", tenant_id=acme["id"])
        platform = await create_key(env.client, "platform-user")

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="header check"),
            headers=auth(member["key"]),
        )
        assert resp.status_code == 200
        assert resp.headers["X-Gateway-Tenant"] == "acme"

        # platform keys carry no tenant header
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="header check"),
            headers=auth(platform["key"]),
        )
        assert resp.status_code == 200
        assert "X-Gateway-Tenant" not in resp.headers


# ----------------------------------------------------------------------
# Cache isolation
# ----------------------------------------------------------------------


async def test_chat_cache_isolation_and_sharing(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        a1 = await create_key(env.client, "a1", tenant_id=acme["id"])
        a2 = await create_key(env.client, "a2", tenant_id=acme["id"])
        # same tenant, but a different response policy (a guardrail
        # disabled) — a cache hit would skip response guardrails, so
        # entries must not cross policies
        a3 = await create_key(
            env.client,
            "a3",
            tenant_id=acme["id"],
            disabled_guardrails=["pii_redaction"],
        )
        b1 = await create_key(env.client, "b1", tenant_id=beta["id"])
        p1 = await create_key(env.client, "p1")

        body = chat_body(content="isolation probe")

        async def _post(key: str) -> httpx.Response:
            return await env.client.post(
                "/v1/chat/completions", json=body, headers=auth(key)
            )

        # tenant A: miss, then hit; a second key in the SAME tenant
        # shares the entry (same tenant, same response policy)
        assert (await _post(a1["key"])).headers["X-Gateway-Cache"] == "MISS"
        assert (await _post(a1["key"])).headers["X-Gateway-Cache"] == "HIT"
        assert (await _post(a2["key"])).headers["X-Gateway-Cache"] == "HIT"

        calls = len(env.mock.requests)

        # tenant B never sees tenant A's entry
        assert (await _post(b1["key"])).headers["X-Gateway-Cache"] == "MISS"
        # platform keys are isolated from tenant entries too (but share
        # among themselves)
        assert (await _post(p1["key"])).headers["X-Gateway-Cache"] == "MISS"
        assert (await _post(p1["key"])).headers["X-Gateway-Cache"] == "HIT"
        # a different response policy misses
        assert (await _post(a3["key"])).headers["X-Gateway-Cache"] == "MISS"

        assert len(env.mock.requests) == calls + 3


async def test_embeddings_cache_tenant_isolation(tmp_path):
    config = make_config(models=[EMB_MODEL], pricing=EMB_PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        a1 = await create_key(env.client, "a1", tenant_id=acme["id"])
        b1 = await create_key(env.client, "b1", tenant_id=beta["id"])
        p1 = await create_key(env.client, "p1")

        body = {"model": "text-embedding-mock", "input": "iso probe"}

        first = await env.client.post("/v1/embeddings", json=body, headers=auth(a1["key"]))
        assert first.status_code == 200
        assert first.headers["X-Gateway-Cache"] == "MISS"

        second = await env.client.post("/v1/embeddings", json=body, headers=auth(a1["key"]))
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "HIT"
        assert second.json() == first.json()

        calls = len(env.mock.requests)
        # another tenant misses (the embeddings cache is tenant-salted —
        # no response guardrails run there, only the data isolation)
        resp = await env.client.post("/v1/embeddings", json=body, headers=auth(b1["key"]))
        assert resp.status_code == 200
        assert resp.headers["X-Gateway-Cache"] == "MISS"
        # platform keys are isolated from tenant entries as well
        resp = await env.client.post("/v1/embeddings", json=body, headers=auth(p1["key"]))
        assert resp.status_code == 200
        assert resp.headers["X-Gateway-Cache"] == "MISS"
        assert len(env.mock.requests) == calls + 2


# ----------------------------------------------------------------------
# Pooled spend, alerts, stats
# ----------------------------------------------------------------------


async def test_tenant_spend_pools_and_alerts(tmp_path):
    sink = WebhookSink()
    alerts = AlertsConfig(webhook_url="http://alerts.test/hook", max_attempts=1)
    async for env in _make_alert_env(tmp_path, alerts, sink):
        acme = await _create_tenant(env.client, "acme", max_budget_usd=30.0)
        k1 = await create_key(env.client, "acme-a", tenant_id=acme["id"])
        k2 = await create_key(env.client, "acme-b", tenant_id=acme["id"])

        # two keys, one pooled budget: $20 + $20 = $40 of $30 (133%)
        for k, content in ((k1, "one"), (k2, "two")):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content=content),
                headers=auth(k["key"]),
            )
            assert resp.status_code == 200
        await _wait_for(lambda: len(sink.payloads) >= 4, what="tenant budget alerts")

        levels = sorted(p["level_percent"] for p in sink.payloads)
        assert levels == [50, 80, 90, 100]  # each exactly once

        # With two concurrent spend tasks (one per key), webhook arrival
        # order is scheduling-dependent — pick payloads by level, not by
        # position. The 50% alert carries whichever task won the
        # fired-levels compare-and-swap: the first committer's $20
        # snapshot, or the second request's $40 one when its task
        # evaluated first. Each level still delivers exactly once with a
        # consistent snapshot; order and which snapshot fires 50% are
        # scheduling, not contract.
        fifty = next(p for p in sink.payloads if p["level_percent"] == 50)
        assert fifty["type"] == "budget_alert"
        assert fifty["kind"] == "tenant"
        assert fifty["key_id"] == f"tenant:{acme['id']}"
        assert fifty["key_name"] == "acme"
        assert fifty["budget_usd"] == 30.0
        assert fifty["spend_usd"] in (20.0, 40.0)
        assert fifty["percent"] in (66.67, 133.33)
        assert "Tenant 'acme'" in fifty["message"]

        exhausted = next(p for p in sink.payloads if p["level_percent"] == 100)
        assert "exhausted" in exhausted["message"]

        # fired levels persist on the tenant row; spend pooled both keys
        row = await _tenant_row(env.client, acme["id"])
        assert row["budget_alerts_fired"] == [50, 80, 90, 100]
        assert row["spend_usd"] == 40.0

        # each key kept its own accounting; neither has a key-level
        # budget, so no key-scoped alert fired
        row_a = await _key_row(env.client, k1["id"])
        row_b = await _key_row(env.client, k2["id"])
        assert row_a["spend_usd"] == 20.0
        assert row_b["spend_usd"] == 20.0
        assert row_a["budget_alerts_fired"] is None
        assert row_b["budget_alerts_fired"] is None


async def test_stats_by_tenant_platform_only(tmp_path):
    async for env, acme, beta, admin, acme_key, beta_key, platform_key in (
        _seed_multi_tenant_traffic(tmp_path)
    ):
        # platform admin: the whole picture, broken down by tenant
        async def _platform_stats() -> dict:
            resp = await env.client.get("/admin/stats?hours=1", headers=auth(ADMIN_KEY))
            assert resp.status_code == 200
            return resp.json()

        async def _enough() -> bool:
            return (await _platform_stats())["total_requests"] >= 2

        await _wait_for(_enough, what="both rows in stats")

        stats = await _platform_stats()
        by_tenant = {e["tenant_id"]: e for e in stats["by_tenant"]}
        assert by_tenant[acme["id"]]["requests"] == 1
        assert by_tenant[beta["id"]]["requests"] == 1
        assert by_tenant[None]["requests"] == 1
        assert stats["total_requests"] == 3

        # tenant admin: own traffic only, no by_tenant breakdown
        resp = await env.client.get("/admin/stats?hours=1", headers=auth(admin["key"]))
        assert resp.status_code == 200
        tstats = resp.json()
        assert tstats["total_requests"] == 1
        assert "by_tenant" not in tstats
        assert {e["key_name"] for e in tstats["by_key"]} == {"acme-member"}
