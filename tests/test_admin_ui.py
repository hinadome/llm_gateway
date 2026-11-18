"""Tests for the admin dashboard (static shell + full-row log detail endpoint)."""

from __future__ import annotations

from .conftest import ADMIN_KEY, auth, chat_body, create_key


async def test_admin_ui_served_without_auth(env):
    resp = await env.client.get("/admin/ui")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    # The shell renders the dashboard tabs and targets the JSON API.
    for marker in (
        "LLM Gateway",
        'id="tab-overview"',
        'id="tab-keys"',
        'id="tab-tenants"',
        'id="tab-vault"',
        'id="tab-prompts"',
        'id="tab-logs"',
        'id="tab-routing"',
        'id="tab-guardrails"',
        'id="shadow-diff-table"',
        "/admin/stats",
        "/admin/keys",
        "/admin/tenants",
        "/admin/prompts",
        "/admin/logs",
        "/admin/routing",
        "/admin/shadow-diff",
        "/admin/guardrails",
    ):
        assert marker in body, marker


async def test_admin_ui_has_no_external_resources(env):
    # The dashboard must work in offline/air-gapped deployments: no CDN
    # scripts, styles or fonts — everything inline.
    body = (await env.client.get("/admin/ui")).text
    assert "http://" not in body
    assert "https://" not in body


async def test_admin_ui_tenants_surface(env):
    # The tenants tab is fully wired: table, create dialog with the
    # tenant fields, and the key dialog's tenancy placement (tenant
    # select + admin-key checkbox). No fetch happens without a key,
    # so only the static shell is assertable here.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="tenants-table"',
        'id="dlg-tenant"',
        'id="t-name"',
        'id="t-budget"',
        'id="t-conc"',
        'id="t-markup"',
        'id="k-conc"',
        'id="k-priority"',
        'id="k-res"',
        'id="k-tools"',
        'id="t-res"',
        'id="t-tools"',
        'id="btn-newtenant"',
        'id="k-tenant"',
        'id="k-admin"',
        "data-tedit",
        "data-tdelete",
        "budgetCellHtml",
        "bootstrapAdminInfo",
    ):
        assert marker in body, marker


async def test_admin_ui_shadow_diff_surface(env):
    # The routing tab carries the tier-35 evidence layer: the aggregate
    # table plus the pair drilldown dialog (verdict badges, load-more,
    # both-sides text). Static-shell assertions only — no fetch
    # happens without a key.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="shadow-diff-table"',
        'id="dlg-shadow-pairs"',
        'id="shadow-pairs-list"',
        'id="shadow-pairs-more"',
        'id="shadow-pairs-title"',
        "loadShadowDiff",
        "openShadowPairs",
        "VERDICT_BADGE",
        "/admin/shadow-diff/pairs",
        "data-pairs-verdict",
    ):
        assert marker in body, marker


async def test_admin_ui_trials_surface(env):
    # Tier 36: the routing tab also carries the candidate-trials
    # panel — the run form (model/candidate selects, window + the
    # three clamped caps) and the runs table whose Pairs buttons feed
    # the same drilldown dialog as shadow pairs.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="trials-table"',
        'id="trial-model"',
        'id="trial-deployment"',
        'id="trial-since"',
        'id="trial-limit"',
        'id="trial-concurrency"',
        'id="trial-cost"',
        'id="btn-newtrial"',
        "loadTrials",
        "/admin/trials",
        "data-trial-pairs",
        "data-trial-verdict",
        "data-trial-cancel",
        "TRIAL_BADGE",
    ):
        assert marker in body, marker


async def test_admin_redirects_to_ui(env):
    resp = await env.client.get("/admin", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/admin/ui"


async def test_shell_public_but_data_protected(env):
    # The shell contains no data and loads without a key; every data
    # endpoint still requires the admin key.
    assert (await env.client.get("/admin/ui")).status_code == 200
    assert (await env.client.get("/admin/keys")).status_code == 401
    assert (await env.client.get("/admin/stats")).status_code == 401
    assert (await env.client.get("/admin/logs")).status_code == 401


async def test_log_detail_includes_bodies(env):
    key = await create_key(env.client, "detail")
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="log detail check"),
        headers=auth(key["key"]),
    )
    assert resp.status_code == 200

    logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
    entry = logs.json()["data"][0]
    # List rows deliberately omit the (potentially large) bodies...
    assert "request_body" not in entry
    assert "response_body" not in entry

    detail = await env.client.get(f"/admin/logs/{entry['id']}", headers=auth(ADMIN_KEY))
    assert detail.status_code == 200
    d = detail.json()
    assert d["id"] == entry["id"]
    assert d["request_body"]["messages"][0]["content"] == "log detail check"
    assert d["response_body"]["choices"]
    assert d["key_name"] == "detail"


async def test_log_detail_404(env):
    resp = await env.client.get("/admin/logs/99999", headers=auth(ADMIN_KEY))
    assert resp.status_code == 404


async def test_log_detail_requires_admin(env):
    plain = await create_key(env.client, "plain")
    assert (await env.client.get("/admin/logs/1")).status_code == 401
    assert (
        await env.client.get("/admin/logs/1", headers=auth(plain["key"]))
    ).status_code == 403
