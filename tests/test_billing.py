"""Usage invoices — tier 45: the billing epic's first surface.

The contract pinned here, in one paragraph: the usage export answers
"which requests did this key make?" one row at a time — chargeback raw
material; the invoice answers what the finance side asks — "what did
each tenant consume this month, per key, per model?" The same
request-log rows rolled up into line items (requests, errors, the
token splits, cost), with the key's and tenant's ``monthly_budget_usd``
beside the spend so a statement doubles as a spend-vs-plan check.
The billing lens is CLIENT-CONSUMED usage: shadow mirrors (never cost
the client anything) and candidate-trial replays (deliberately re-run
traffic) are excluded, while cache hits ride along — a cached answer
is a real served request, already billed at the cached rate. Keys
with no traffic still appear (an unused key is a finops finding, not
an absence); keys whose row is gone keep their line items under the
tenant they lived in, with no budget to compare against. Calendar
months in UTC, ``month=YYYY-MM``; a future month is a 400 — an
invoice for a period that has not happened must not pretend to be
empty. Tenant admins invoice only their own tenant (forced); plain
keys get the admin 403; platform admins may filter ``?tenant_id=``.
Everything sorts by spend — the big numbers are the top of the
statement — and ``format=csv`` is one row per line item in a stable
column order, served as a download attachment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from gateway.core import billing
from gateway.core.billing import INVOICE_CSV_COLUMNS, parse_month, rollup
from gateway.db import get_sessionmaker
from gateway.errors import GatewayError
from gateway.models import RequestLog, VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, create_key
from .test_tenants import _create_tenant

_NOW = datetime.now(timezone.utc)
_THIS_MONTH = f"{_NOW.year:04d}-{_NOW.month:02d}"


# -- helpers ---------------------------------------------------------------


def _log(
    request_id: str,
    *,
    key_id: str | None = None,
    key_name: str | None = None,
    tenant_id: str | None = None,
    model: str = "test-model",
    status: str = "success",
    prompt: int = 0,
    completion: int = 0,
    cached: int = 0,
    total: int | None = None,
    cost: float = 0.0,
    cache_hit: bool = False,
    shadow: bool = False,
    trial_id: str | None = None,
    created_at: datetime | None = None,
) -> RequestLog:
    """One request-log row. ``created_at`` defaults to two minutes ago
    (inside every window, tz-aware — the suite's seeding convention)."""
    return RequestLog(
        request_id=request_id,
        key_id=key_id,
        key_name=key_name,
        tenant_id=tenant_id,
        model=model,
        status=status,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        total_tokens=total if total is not None else prompt + completion,
        cost_usd=cost,
        cache_hit=cache_hit,
        shadow=shadow or None,
        trial_id=trial_id,
        created_at=created_at
        or (datetime.now(timezone.utc) - timedelta(minutes=2)),
    )


async def _seed(*rows: RequestLog) -> None:
    async with get_sessionmaker()() as session:
        for row in rows:
            session.add(row)
        await session.commit()


# -- pure units --------------------------------------------------------------


def test_parse_month_bounds_and_rejections():
    """Calendar months in UTC: explicit ``YYYY-MM`` to exact bounds, the
    year rolls over, ``None`` means the current month, and everything
    malformed — or in the future — is a 400 naming the ``month`` param.
    A period that has not happened must not pretend to be an empty
    invoice."""
    month, since, until = parse_month("2026-10")
    assert month == "2026-10"
    assert since.isoformat() == "2026-10-01T00:00:00+00:00"
    assert until.isoformat() == "2026-11-01T00:00:00+00:00"

    # Year rollover: December's "next" is January of the next year.
    _, since, until = parse_month("2025-12")
    assert until.isoformat() == "2026-01-01T00:00:00+00:00"

    # None = the current UTC month, and it contains now by construction.
    month, since, until = parse_month(None)
    assert month == _THIS_MONTH
    assert since <= datetime.now(timezone.utc) < until

    for bad, why in (
        ("november", "not YYYY-MM"),
        ("2026-0", "month 0"),
        ("2026-13", "month 13"),
        ("2030-01", "the future"),
    ):
        with pytest.raises(GatewayError) as exc:
            parse_month(bad)
        assert exc.value.status_code == 400, why
        assert "Invalid month" in exc.value.message


def test_rollup_groups_keys_into_tenants():
    """The pure leg: line items into keys, keys into tenants — cost-desc
    at every level; keys with no traffic included as zero-spend
    sections; traffic whose key row is gone keeps its line items under
    the row's tenant with no budget; unassigned (platform) keys are a
    separate bucket; budget_pct only when there is a budget to compare
    against; totals over everything."""
    lines = [
        # alpha (tenant t1): two models — the expensive one first.
        {
            "tenant_id": "t1", "key_id": "k1", "key_name": "alpha",
            "model": "gpt-4o", "requests": 10, "errors": 1,
            "prompt_tokens": 100, "completion_tokens": 50,
            "cached_tokens": 20, "total_tokens": 150, "cost_usd": 0.5,
        },
        {
            "tenant_id": "t1", "key_id": "k1", "key_name": "alpha",
            "model": "haiku", "requests": 5, "errors": 0,
            "prompt_tokens": 20, "completion_tokens": 10,
            "cached_tokens": 0, "total_tokens": 30, "cost_usd": 0.01,
        },
        # A key row that no longer exists: its logs still bill under t1.
        {
            "tenant_id": "t1", "key_id": "gone", "key_name": "deleted-key",
            "model": "gpt-4o", "requests": 2, "errors": 0,
            "prompt_tokens": 10, "completion_tokens": 5,
            "cached_tokens": 0, "total_tokens": 15, "cost_usd": 0.3,
        },
        # A platform key (no tenant).
        {
            "tenant_id": None, "key_id": "k2", "key_name": "orphan",
            "model": "gpt-4o", "requests": 1, "errors": 0,
            "prompt_tokens": 5, "completion_tokens": 5,
            "cached_tokens": 0, "total_tokens": 10, "cost_usd": 0.99,
        },
    ]
    keys_by_id = {
        "k1": {
            "name": "alpha", "tenant_id": "t1", "monthly_budget_usd": 10.0,
        },
        # A key with no traffic this period: still on the statement.
        "k3": {
            "name": "unused", "tenant_id": "t1", "monthly_budget_usd": None,
        },
    }
    tenants_by_id = {"t1": {"name": "Acme", "monthly_budget_usd": 20.0}}

    statement = rollup(lines, keys_by_id, tenants_by_id)

    # One tenant section, carrying its pooled budget beside the spend.
    (tenant,) = statement["tenants"]
    assert tenant["tenant_id"] == "t1"
    assert tenant["tenant_name"] == "Acme"
    assert tenant["cost_usd"] == 0.81  # 0.5 + 0.01 + 0.3
    assert tenant["budget_pct"] == 4.05  # 0.81 / 20.0
    assert tenant["requests"] == 17
    # Keys sorted by spend: alpha (0.51) > deleted-key (0.3) > unused (0).
    assert [k["key_name"] for k in tenant["keys"]] == [
        "alpha", "deleted-key", "unused",
    ]
    alpha = tenant["keys"][0]
    # Line items sorted by spend; the totals are the section's own.
    assert [(li["model"], li["cost_usd"]) for li in alpha["line_items"]] == [
        ("gpt-4o", 0.5), ("haiku", 0.01),
    ]
    assert alpha["monthly_budget_usd"] == 10.0
    assert alpha["budget_pct"] == 5.1  # 0.51 / 10.0
    assert alpha["total_tokens"] == 180
    # The deleted key: line items kept, no budget to compare against.
    gone = tenant["keys"][1]
    assert gone["key_id"] == "gone"
    assert gone["monthly_budget_usd"] is None
    assert gone["budget_pct"] is None
    assert gone["cost_usd"] == 0.3
    # The unused key: a zero-spend section with no line items.
    unused = tenant["keys"][2]
    assert unused["requests"] == 0
    assert unused["cost_usd"] == 0.0
    assert unused["line_items"] == []
    assert unused["budget_pct"] is None

    # Platform keys live outside the tenant sections.
    assert [k["key_name"] for k in statement["unassigned"]] == ["orphan"]
    assert statement["unassigned"][0]["cost_usd"] == 0.99

    assert statement["totals"] == {
        "requests": 18,
        "errors": 1,
        "prompt_tokens": 135,
        "completion_tokens": 70,
        "cached_tokens": 20,
        "total_tokens": 205,
        "cost_usd": 1.8,
    }


def test_invoice_csv_columns_are_a_stable_contract():
    """The column order downstream finance tooling codes against."""
    assert INVOICE_CSV_COLUMNS == (
        "month", "tenant_id", "tenant_name", "key_id", "key_name",
        "model", "requests", "errors", "prompt_tokens",
        "completion_tokens", "cached_tokens", "total_tokens", "cost_usd",
    )


# -- e2e: the statement ------------------------------------------------------


async def test_invoice_statement_pin(tmp_path):
    """The full statement, pinned end to end: two tenants (one with a
    pooled budget), a key with two models and a cache-hit row (cached
    answers are real served requests — included), an error row, a
    zero-usage key (a finops finding, not an absence), a key whose row
    was deleted (line items kept under its tenant, no budget), a
    platform key and an admin-key row (no key at all) in the unassigned
    bucket — everything sorted by spend, budgets compared, totals
    exact."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(
            env.client, "Acme", monthly_budget_usd=20.0
        )
        globex = await _create_tenant(env.client, "Globex")
        alpha = await create_key(
            env.client, "alpha",
            tenant_id=acme["id"], monthly_budget_usd=10.0,
        )
        await create_key(env.client, "beta", tenant_id=acme["id"])
        gamma = await create_key(
            env.client, "gamma",
            tenant_id=acme["id"], monthly_budget_usd=5.0,
        )
        delta = await create_key(env.client, "delta", tenant_id=globex["id"])
        epsilon = await create_key(env.client, "epsilon")

        # alpha: two models — test-model carries an error row and a
        # cache-hit row (both billed — the cache hit at its cached rate).
        await _seed(
            _log(
                "a1", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=40, completion=20, cost=0.30,
            ),
            _log(
                "a2", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], status="error",
                prompt=30, completion=10, cost=0.20,
            ),
            _log(
                "a3", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=30, completion=20,
                cached=20, cost=0.001, cache_hit=True,
            ),
            _log(
                "a4", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], model="other-model",
                prompt=20, completion=10, cost=0.01,
            ),
        )
        # gamma: traffic, then the key row is deleted outright (the
        # log-retention-outlives-the-key story).
        await _seed(
            _log(
                "g1", key_id=gamma["id"], key_name="gamma",
                tenant_id=acme["id"], prompt=10, completion=5, cost=0.889,
            ),
        )
        async with get_sessionmaker()() as session:
            await session.execute(
                delete(VirtualKey).where(VirtualKey.id == gamma["id"])
            )
            await session.commit()
        # delta (Globex), epsilon (platform key), and one admin-key
        # row — no key row at all.
        await _seed(
            _log(
                "d1", key_id=delta["id"], key_name="delta",
                tenant_id=globex["id"], model="other-model",
                prompt=50, completion=25, cost=1.2,
            ),
            _log(
                "e1", key_id=epsilon["id"], key_name="epsilon",
                prompt=5, completion=5, cost=0.05,
            ),
            _log("admin-1", prompt=1, completion=1, cost=0.02),
        )

        resp = await env.client.get("/admin/invoices", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "invoice"
        assert body["month"] == _THIS_MONTH
        assert body["period"]["since"].endswith("-01T00:00:00+00:00")
        assert "generated_at" in body

        # Tenants by spend: Acme (1.4) before Globex (1.2).
        assert [t["tenant_name"] for t in body["tenants"]] == ["Acme", "Globex"]
        acme_section, globex_section = body["tenants"]

        assert acme_section["cost_usd"] == 1.4
        assert acme_section["budget_pct"] == 7.0  # 1.4 / 20.0
        assert acme_section["requests"] == 5
        # Keys by spend: gamma (0.889) > alpha (0.511) > beta (0).
        assert [k["key_name"] for k in acme_section["keys"]] == [
            "gamma", "alpha", "beta",
        ]
        gamma_key, alpha_key, beta_key = acme_section["keys"]

        # The deleted key: line items kept under its tenant, no budget.
        assert gamma_key["key_id"] == gamma["id"]
        assert gamma_key["monthly_budget_usd"] is None
        assert gamma_key["budget_pct"] is None
        assert gamma_key["cost_usd"] == 0.889

        # alpha: two line items by spend, the cache hit and the error
        # both inside the test-model line, budget compared.
        assert [
            (li["model"], li["cost_usd"]) for li in alpha_key["line_items"]
        ] == [("test-model", 0.501), ("other-model", 0.01)]
        test_line = alpha_key["line_items"][0]
        assert test_line["requests"] == 3
        assert test_line["errors"] == 1
        assert test_line["cached_tokens"] == 20
        assert alpha_key["requests"] == 4
        assert alpha_key["errors"] == 1
        assert alpha_key["prompt_tokens"] == 120
        assert alpha_key["completion_tokens"] == 60
        assert alpha_key["total_tokens"] == 180
        assert alpha_key["monthly_budget_usd"] == 10.0
        assert alpha_key["budget_pct"] == 5.11

        # The zero-usage key: present, zero, no line items.
        assert beta_key["requests"] == 0
        assert beta_key["cost_usd"] == 0.0
        assert beta_key["line_items"] == []

        # Globex has no budget: pct is None, never a fake zero.
        assert globex_section["cost_usd"] == 1.2
        assert globex_section["monthly_budget_usd"] is None
        assert globex_section["budget_pct"] is None

        # Unassigned: the platform key, then the admin-key row (no key
        # row at all) — both outside the tenant sections.
        assert [k["key_name"] for k in body["unassigned"]] == [
            "epsilon", None,
        ]
        assert body["unassigned"][0]["line_items"][0]["tenant_id"] is None
        assert body["unassigned"][1]["key_id"] is None

        assert body["totals"] == {
            "requests": 8,
            "errors": 1,
            "prompt_tokens": 186,
            "completion_tokens": 96,
            "cached_tokens": 20,
            "total_tokens": 282,
            "cost_usd": 2.67,
        }
        break


async def test_invoice_excludes_shadow_and_trial(tmp_path):
    """The billing lens is client-consumed usage: shadow mirrors never
    cost the client anything, and candidate-trial replays are
    deliberately re-run traffic — neither bills the tenant, no matter
    how expensive the rows look."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        alpha = await create_key(env.client, "alpha", tenant_id=acme["id"])

        await _seed(
            _log(
                "real-1", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=10, completion=5, cost=0.10,
            ),
            _log(
                "shadow-1", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=500, completion=500,
                cost=99.0, shadow=True,
            ),
            _log(
                "trial-1", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=300, completion=300,
                cost=55.0, trial_id="trial-1",
            ),
        )

        resp = await env.client.get("/admin/invoices", headers=auth(ADMIN_KEY))
        body = resp.json()
        (tenant,) = body["tenants"]
        assert tenant["cost_usd"] == 0.10
        assert tenant["requests"] == 1
        (key,) = tenant["keys"]
        assert key["prompt_tokens"] == 10
        assert key["total_tokens"] == 15
        assert body["totals"]["cost_usd"] == 0.10
        break


# -- e2e: the period ---------------------------------------------------------


async def test_invoice_month_scoping_and_rejections(tmp_path):
    """Calendar months in UTC: rows land in the month they happened,
    ``month=YYYY-MM`` selects the period, and a malformed or future
    month is the app's 400 envelope (naming the param) — never a 422,
    never a fake empty invoice for a period that has not happened."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        alpha = await create_key(env.client, "alpha", tenant_id=acme["id"])
        october = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        await _seed(
            _log(
                "oct-1", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=10, completion=5, cost=1.0,
                created_at=october,
            ),
            _log(
                "now-1", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=10, completion=5, cost=0.25,
                created_at=datetime.now(timezone.utc),
            ),
        )

        # Default month: today's rows, not October's.
        body = (
            await env.client.get("/admin/invoices", headers=auth(ADMIN_KEY))
        ).json()
        assert body["month"] == _THIS_MONTH
        assert body["totals"]["requests"] == 1
        assert body["totals"]["cost_usd"] == 0.25

        # October: last month's rows, and the period says so exactly.
        resp = await env.client.get(
            "/admin/invoices",
            params={"month": "2026-10"},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["month"] == "2026-10"
        assert body["period"] == {
            "since": "2026-10-01T00:00:00+00:00",
            "until": "2026-11-01T00:00:00+00:00",
        }
        assert body["totals"]["requests"] == 1
        assert body["totals"]["cost_usd"] == 1.0

        # Invalid months: the app's 400 envelope, not FastAPI's 422.
        for bad, message in (
            ("november", "Invalid month: expected YYYY-MM."),
            ("2026-13", "Invalid month: expected YYYY-MM."),
            ("2030-01", "Invalid month: must not be in the future."),
        ):
            resp = await env.client.get(
                "/admin/invoices",
                params={"month": bad},
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 400, bad
            error = resp.json()["error"]
            assert error["message"] == message
            assert error["type"] == "invalid_request_error"
        break


# -- e2e: who may read which statement ----------------------------------------


async def test_invoice_scoping(tmp_path):
    """Tenant admins invoice only their own tenant — even when they ask
    for another (the scope is forced, and platform keys are not
    theirs); plain keys get the admin 403; platform admins may filter
    ``?tenant_id=`` or read the whole platform."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        globex = await _create_tenant(env.client, "Globex")
        acme_admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        member = await create_key(env.client, "member", tenant_id=acme["id"])
        alpha = await create_key(env.client, "alpha", tenant_id=acme["id"])
        delta = await create_key(env.client, "delta", tenant_id=globex["id"])

        await _seed(
            _log(
                "a", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], cost=1.0,
            ),
            _log(
                "d", key_id=delta["id"], key_name="delta",
                tenant_id=globex["id"], cost=2.0,
            ),
            _log("p", key_name="platform", cost=4.0),
        )

        # A plain key: not an admin at all.
        resp = await env.client.get(
            "/admin/invoices", headers=auth(member["key"])
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["message"] == (
            "Admin access required for this endpoint."
        )

        # The tenant admin: their tenant only — even when asking for
        # Globex, and the unassigned platform rows are not theirs.
        # Every acme key appears (zero-usage sections included — an
        # unused key is a finops finding), sorted by spend.
        resp = await env.client.get(
            "/admin/invoices",
            params={"tenant_id": globex["id"]},
            headers=auth(acme_admin["key"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [t["tenant_name"] for t in body["tenants"]] == ["Acme"]
        assert [k["key_name"] for k in body["tenants"][0]["keys"]] == [
            "alpha", "acme-admin", "member",
        ]
        assert body["unassigned"] == []
        assert body["totals"]["cost_usd"] == 1.0

        # The platform admin: one tenant on request...
        body = (
            await env.client.get(
                "/admin/invoices",
                params={"tenant_id": globex["id"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        assert [t["tenant_name"] for t in body["tenants"]] == ["Globex"]
        assert body["totals"]["cost_usd"] == 2.0

        # ...and the whole platform (tenants + unassigned) without one.
        body = (
            await env.client.get(
                "/admin/invoices", headers=auth(ADMIN_KEY)
            )
        ).json()
        assert [t["tenant_name"] for t in body["tenants"]] == [
            "Globex", "Acme",
        ]
        assert [k["key_name"] for k in body["unassigned"]] == ["platform"]
        assert body["totals"]["cost_usd"] == 7.0
        break


# -- e2e: the CSV ------------------------------------------------------------


async def test_invoice_csv(tmp_path):
    """``format=csv`` is one row per line item in the stable column
    order, served as a download attachment named for the month — the
    same statement the JSON tells, reshaped for a spreadsheet."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        alpha = await create_key(env.client, "alpha", tenant_id=acme["id"])
        await _seed(
            _log(
                "a1", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], prompt=40, completion=20, cost=0.30,
            ),
            _log(
                "a2", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], model="other-model",
                prompt=10, completion=5, cost=0.01,
            ),
            _log("p", key_name="platform", cost=0.02),
        )

        resp = await env.client.get(
            "/admin/invoices",
            params={"format": "csv", "month": "2026-10"},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert resp.headers["content-disposition"] == (
            'attachment; filename="invoice_2026-10.csv"'
        )
        # month=2026-10 has no rows here — but the header always ships.
        lines = resp.text.strip().splitlines()
        assert lines[0] == ",".join(INVOICE_CSV_COLUMNS)
        assert len(lines) == 1  # the requested month had no traffic

        # The current month: one row per line item, tenants then
        # unassigned, values in column order.
        resp = await env.client.get(
            "/admin/invoices", params={"format": "csv"}, headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == (
            f'attachment; filename="invoice_{_THIS_MONTH}.csv"'
        )
        lines = resp.text.strip().splitlines()
        assert lines[0] == ",".join(INVOICE_CSV_COLUMNS)
        assert len(lines) == 4  # header + 2 line items + platform row
        (acme_row, other_row, platform_row) = (
            lines[1].split(","), lines[2].split(","), lines[3].split(",")
        )
        assert acme_row == [
            _THIS_MONTH, acme["id"], "Acme", alpha["id"], "alpha",
            "test-model", "1", "0", "40", "20", "0", "60", "0.3",
        ]
        assert other_row[5] == "other-model"
        assert other_row[12] == "0.01"
        assert platform_row == [
            _THIS_MONTH, "", "", "", "platform",
            "test-model", "1", "0", "0", "0", "0", "0", "0.02",
        ]
        break


# -- e2e: real traffic --------------------------------------------------------


async def test_invoice_bills_real_traffic(tmp_path):
    """A request that really went through the pipeline bills on the
    invoice with the calculator's own numbers — default pricing
    (model-a: $2/M input, $4/M output), the mock's 10/7 token usage —
    under the public model name the client asked for."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        alpha = await create_key(env.client, "alpha", tenant_id=acme["id"])

        resp = await env.client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(alpha["key"]),
        )
        assert resp.status_code == 200, resp.text

        body = (
            await env.client.get("/admin/invoices", headers=auth(ADMIN_KEY))
        ).json()
        (tenant,) = body["tenants"]
        assert tenant["tenant_name"] == "Acme"
        (key,) = tenant["keys"]
        assert key["key_name"] == "alpha"
        (line,) = key["line_items"]
        assert line["model"] == "test-model"  # the public name
        assert line["requests"] == 1
        assert line["prompt_tokens"] == 10
        assert line["completion_tokens"] == 7
        assert line["total_tokens"] == 17
        assert line["cost_usd"] == pytest.approx((10 * 2.0 + 7 * 4.0) / 1_000_000)
        assert body["totals"]["cost_usd"] == pytest.approx(
            (10 * 2.0 + 7 * 4.0) / 1_000_000
        )
        break
