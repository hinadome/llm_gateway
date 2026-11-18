"""Tenant pricing markup — cost-plus billing for resellers/chargeback.

A tenant can carry ``pricing_markup_pct`` (e.g. 20.0 = bill member-key
traffic at 1.2x provider cost). The **billed** cost is what spend,
budgets, log rows, the usage export, the per-key metrics and the
X-Gateway-Cost-Usd header record — the chargeback number — while
``gateway_cost_usd_total{model}`` keeps the ACTUAL provider cost so
platform economics stay visible beside tenant economics, and the new
``gateway_markup_usd_total{tenant}`` series counts the markup revenue
itself.

Applies to every modality (chat, embeddings — the token path; images,
audio, speech — the unit-priced path) and to every member key, tenant
admins included. Snapshotted at auth time like the other tenant
ceilings: a PATCH applies to the next request, and platform keys
(no tenant) always bill at actual cost.
"""

from __future__ import annotations

import pytest
from prometheus_client.registry import REGISTRY

from gateway.config import MetricsConfig

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _wait_for

# Pricing that makes one non-stream mock request (10 prompt + 7
# completion tokens) cost $0.048 — big enough that 6-decimal admin
# roundings (exports, stats) stay exact: 0.048 actual, 0.0576 billed
# at +20%, 0.0096 markup revenue.
PRICING = {"model-a": {"input": 2000.0, "output": 4000.0}}

ACTUAL = 0.048
BILLED_20 = 0.0576  # ACTUAL * 1.2
MARKUP_20 = 0.0096


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _create_tenant(client, name: str, **fields) -> dict:
    resp = await client.post(
        "/admin/tenants", json={"name": name, **fields}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _tenant_row(client, tenant_id: str) -> dict:
    resp = await client.get(f"/admin/tenants/{tenant_id}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _key_row(client, key_id: str) -> dict:
    resp = await client.get(f"/admin/keys/{key_id}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ----------------------------------------------------------------------
# Billed vs actual, end to end
# ----------------------------------------------------------------------


async def test_markup_bills_log_header_spend_and_export(tmp_path):
    config = make_config(pricing=PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", pricing_markup_pct=20.0)
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        cost_before = _sample("gateway_cost_usd_total", {"model": "test-model"})
        markup_before = _sample("gateway_markup_usd_total", {"tenant": "acme"})

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello marked-up world"),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 200, resp.text
        # The caller-facing cost header is the BILLED cost (what the
        # tenant pays), same number the log row records.
        assert resp.headers["x-gateway-cost-usd"] == f"{BILLED_20:.8f}"

        # The log row: billed cost, tenant-stamped.
        row = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"][0]
        assert row["tenant_id"] == acme["id"]
        assert row["cost_usd"] == pytest.approx(BILLED_20)

        # Platform economics stay actual: the model-labeled series does
        # not move by the marked-up amount, and the markup revenue is
        # counted on its own series.
        assert _sample("gateway_cost_usd_total", {"model": "test-model"}) - (
            cost_before
        ) == pytest.approx(ACTUAL)
        assert _sample("gateway_markup_usd_total", {"tenant": "acme"}) - (
            markup_before
        ) == pytest.approx(MARKUP_20)

        # Spend pools the BILLED cost on both the key and the tenant.
        async def _spent() -> bool:
            k = await _key_row(env.client, key["id"])
            t = await _tenant_row(env.client, acme["id"])
            return (
                k["spend_usd"] == pytest.approx(BILLED_20)
                and t["spend_usd"] == pytest.approx(BILLED_20)
            )

        await _wait_for(_spent, what="billed spend recorded on key + tenant")

        # The chargeback artifacts export the billed cost: the summary
        # aggregate, the per-tenant group and the row list all carry it.
        export = await env.client.get(
            "/admin/usage/export", params={"format": "json"}, headers=auth(ADMIN_KEY)
        )
        assert export.status_code == 200
        summary = export.json()["summary"]
        assert summary["cost_usd"] == pytest.approx(BILLED_20)
        by_tenant = {
            e["tenant_id"]: e["cost_usd"] for e in summary["by_tenant"]
        }
        assert by_tenant[acme["id"]] == pytest.approx(BILLED_20)
        assert export.json()["requests"][0]["cost_usd"] == pytest.approx(BILLED_20)


async def test_per_key_metrics_bill_marked_up_cost(tmp_path):
    # metrics.per_key is the chargeback dashboard — it reports the
    # BILLED cost, consistent with spend and the log row.
    config = make_config(pricing=PRICING, metrics=MetricsConfig(per_key=True))
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", pricing_markup_pct=20.0)
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        before = _sample("gateway_key_cost_usd_total", {"key": "acme-member"})
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="chargeback view"),
            headers=auth(key["key"]),
        )
        assert (
            _sample("gateway_key_cost_usd_total", {"key": "acme-member"}) - before
        ) == pytest.approx(BILLED_20)


async def test_markup_applies_to_unit_priced_modalities(tmp_path):
    # Images bill per unit (image_usd) through _account_cost — the
    # other accounting path — and get marked up too.
    config = make_config(pricing={"model-a": {"image_usd": 1.0}})
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", pricing_markup_pct=20.0)
        key = await create_key(env.client, "acme-artist", tenant_id=acme["id"])

        resp = await env.client.post(
            "/v1/images/generations",
            json={"model": "test-model", "prompt": "a cat"},
            headers=auth(key["key"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-cost-usd"] == f"{1.2:.8f}"

        async def _spent() -> bool:
            t = await _tenant_row(env.client, acme["id"])
            return t["spend_usd"] == pytest.approx(1.2)

        await _wait_for(_spent, what="marked-up image spend recorded")


async def test_platform_keys_bill_at_actual_cost(tmp_path):
    # No tenant -> no markup, ever. The billed cost equals the provider
    # cost, and no markup series is touched.
    config = make_config(pricing=PRICING)
    async for env in _make_env(tmp_path, config=config):
        key = await create_key(env.client, "platform-key")
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="platform traffic"),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-cost-usd"] == f"{ACTUAL:.8f}"
        row = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"][0]
        assert row["cost_usd"] == pytest.approx(ACTUAL)


async def test_zero_and_unset_markup_bill_actual(tmp_path):
    # NULL (never set) and an explicit 0 are both "bill at actual".
    config = make_config(pricing=PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme")  # no markup
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="unset markup"),
            headers=auth(key["key"]),
        )
        assert resp.headers["x-gateway-cost-usd"] == f"{ACTUAL:.8f}"

        patched = await env.client.patch(
            f"/admin/tenants/{acme['id']}",
            json={"pricing_markup_pct": 0.0},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["pricing_markup_pct"] == 0.0

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="explicit zero"),
            headers=auth(key["key"]),
        )
        assert resp.headers["x-gateway-cost-usd"] == f"{ACTUAL:.8f}"


async def test_markup_patch_applies_on_next_request(tmp_path):
    # The markup snapshots at auth time like every other tenant
    # ceiling: a PATCH shows up on the next request, not mid-flight.
    config = make_config(pricing=PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme")
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="before patch"),
            headers=auth(key["key"]),
        )
        assert resp.headers["x-gateway-cost-usd"] == f"{ACTUAL:.8f}"

        patched = await env.client.patch(
            f"/admin/tenants/{acme['id']}",
            json={"pricing_markup_pct": 100.0},  # 2x — unmistakable
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="after patch"),
            headers=auth(key["key"]),
        )
        assert resp.headers["x-gateway-cost-usd"] == f"{ACTUAL * 2:.8f}"


async def test_budgets_enforce_the_billed_cost(tmp_path):
    # The discriminator: actual cost ($0.048) fits the $0.05 ceiling,
    # billed cost ($0.0576) does not. If budgets were actual-based the
    # second request would pass; billed-based, it fails closed.
    config = make_config(pricing=PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(
            env.client, "budgeted", pricing_markup_pct=20.0, max_budget_usd=0.05
        )
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="first"),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 200

        async def _spent() -> bool:
            t = await _tenant_row(env.client, acme["id"])
            return t["spend_usd"] == pytest.approx(BILLED_20)

        await _wait_for(_spent, what="billed spend recorded")

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="second"),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 429
        err = resp.json()["error"]
        assert err["code"] == "insufficient_quota"
        assert "Tenant 'budgeted' has exceeded its budget" in err["message"]


async def test_tenant_admin_billed_at_tenant_markup(tmp_path):
    # "The tenant's ceiling applies to every member key, tenant admins
    # included" — the markup is no exception: an admin key inside the
    # tenant bills at the tenant's marked-up rate.
    config = make_config(pricing=PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", pricing_markup_pct=20.0)
        admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="tenant admin traffic"),
            headers=auth(admin["key"]),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-cost-usd"] == f"{BILLED_20:.8f}"


async def test_streaming_bills_marked_up_cost(tmp_path):
    # Streams account through the same _account_usage tail — the
    # billed cost lands on the log row. The mock's stream usage chunk
    # reports 5 prompt + 2 completion tokens ($0.018 at PRICING), so
    # the billed row cost is $0.0216 at +20%.
    STREAM_ACTUAL = 0.018
    STREAM_BILLED = STREAM_ACTUAL * 1.2
    config = make_config(pricing=PRICING)
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", pricing_markup_pct=20.0)
        key = await create_key(env.client, "acme-member", tenant_id=acme["id"])

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="streaming marked up", stream=True),
            headers=auth(key["key"]),
        )
        assert resp.status_code == 200
        await resp.aread()

        async def _logged() -> bool:
            rows = (
                await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
            ).json()["data"]
            return bool(rows) and rows[0]["cost_usd"] is not None

        await _wait_for(_logged, what="stream row logged")
        row = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"][0]
        assert row["stream"] is True
        assert row["cost_usd"] == pytest.approx(STREAM_BILLED)


# ----------------------------------------------------------------------
# Admin surface + validation
# ----------------------------------------------------------------------


async def test_markup_api_surface_and_validation(tmp_path):
    async for env in _make_env(tmp_path):
        # Validation: a markup is a non-negative percent, bounded. Body
        # validation surfaces as the OpenAI-style 400 (not FastAPI's 422).
        for bad in (-5.0, 1001.0):
            resp = await env.client.post(
                "/admin/tenants",
                json={"name": "bad", "pricing_markup_pct": bad},
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 400, bad

        # Create with markup -> visible on the row; None = unset.
        acme = await _create_tenant(env.client, "acme", pricing_markup_pct=20.0)
        assert acme["pricing_markup_pct"] == 20.0
        beta = await _create_tenant(env.client, "beta")
        assert beta["pricing_markup_pct"] is None

        # PATCH updates it; listed rows carry it.
        patched = await env.client.patch(
            f"/admin/tenants/{acme['id']}",
            json={"pricing_markup_pct": 50.0},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["pricing_markup_pct"] == 50.0

        listed = {
            t["name"]: t
            for t in (
                await env.client.get("/admin/tenants", headers=auth(ADMIN_KEY))
            ).json()["data"]
        }
        assert listed["acme"]["pricing_markup_pct"] == 50.0
        assert listed["beta"]["pricing_markup_pct"] is None
