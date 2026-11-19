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
        'id="tab-feedback"',
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


async def test_admin_ui_migration_verdicts_surface(env):
    # Tier 37: the routing tab's evidence tables carry the go/no-go
    # recommendations — the shadow-diff table fetches
    # /admin/migration/verdicts in parallel (one badge per mirrored
    # candidate) and the trials table renders each run's folded
    # recommendation; both tooltip their failed-check reasons.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        "/admin/migration/verdicts",
        "MIGRATION_BADGE",
        "Go / no-go",
    ):
        assert marker in body, marker


async def test_admin_ui_promotion_surface(env):
    # Tier 38: every shadow-diff row carries a Promote… button opening
    # the promotion dialog — weight input + force checkbox, a plan
    # preview that re-plans on every weight change (POST
    # /admin/migration/plan, the diff is the preview) and the apply
    # (POST /admin/migration/promote) that toasts and refreshes the
    # routing tables. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="dlg-promotion"',
        'id="form-promotion"',
        'id="promotion-weight"',
        'id="promotion-force"',
        'id="promotion-plan"',
        'id="promotion-err"',
        "data-promote-model",
        "openPromotion",
        "loadPromotionPlan",
        "/admin/migration/plan",
        "/admin/migration/promote",
        "Promote&hellip;",
    ):
        assert marker in body, marker


async def test_admin_ui_rollback_surface(env):
    # Tier 39: the Routing tab's Backups button opens the promotion
    # backups dialog — one row per config.yaml.bak-<stamp> with the
    # parsed what-rolling-back-changes story, a Preview that renders
    # the detail diff, and the Rollback-to-this apply (POST
    # /admin/migration/rollback) that toasts the undo point and
    # refreshes the routing tables. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="dlg-backups"',
        'id="backups-table"',
        'id="backups-preview"',
        'id="backups-diff"',
        'id="backups-err"',
        'id="btn-backups"',
        "data-backup-preview",
        "data-backup-rollback",
        "loadBackups",
        "rollbackChangeText",
        "/admin/migration/backups",
        "/admin/migration/rollback",
    ):
        assert marker in body, marker


async def test_admin_ui_watch_surface(env):
    # Tier 40: the Routing tab's Post-promotion watch table — serving
    # traffic re-verdicted against the migration thresholds (GET
    # /admin/migration/watch): the go/no-go badge with the
    # failed-check reasons in its tooltip, the expected -> actual
    # share split, requests/errors/latency/cost means, the latency
    # delta vs the tier pulled from recommendation.checks by name,
    # and the watching-since column. Loaded by loadRouting after the
    # shadow-diff table. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="watch-table"',
        "Post-promotion watch",
        "loadWatch",
        "/admin/migration/watch",
        "latency_delta_pct",
    ):
        assert marker in body, marker


async def test_admin_ui_playground_surface(env):
    # Tier 41: the Playground tab — the dashboard's own chat client
    # (POST /admin/playground/chat): model dropdown from /admin/routing
    # and key dropdown from /admin/keys (empty = the admin's own
    # context), max-tokens/temperature knobs, a client-side conversation
    # list where each Send posts the whole history, and a diagnostics
    # panel rendering the request-log row the run just wrote (key,
    # route/status, deployment, provider, cache hit, tokens, cost,
    # latency, guardrail verdicts) — with the error envelope a real
    # client would receive on failure. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'data-tab="playground"',
        'id="tab-playground"',
        'id="pg-model"',
        'id="pg-key"',
        'id="pg-max-tokens"',
        'id="pg-temp"',
        'id="pg-send"',
        'id="pg-messages"',
        'id="pg-input"',
        'id="pg-diagnostics"',
        'id="pg-clear"',
        "loadPlayground",
        "/admin/playground/chat",
        "auth_method",
        "guardrail_actions",
    ):
        assert marker in body, marker


async def test_admin_ui_slo_surface(env):
    # Tier 42: the Overview tab's SLO panel — the operator's named
    # promises from `slos:` in config.yaml, verdicted (GET /admin/slos,
    # platform admin): objective/scope/metric columns, the bar and the
    # measured value beside it, the request count against the
    # min_requests sufficiency gate, the met/breached/insufficient
    # badge with the failed reason in its tooltip, each target's own
    # rolling window. The panel hides itself when no targets are
    # configured or when a tenant admin cannot read them. Static-shell
    # assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="ov-slos-panel"',
        'id="ov-slos"',
        "loadSLOs",
        "/admin/slos",
        "SLO_BADGE",
        "insufficient_evidence",
        "fmtSloValue",
        "min_requests",
    ):
        assert marker in body, marker


async def test_admin_ui_slo_budget_surface(env):
    # Tier 43: the SLO panel's error-budget column — the bar restated
    # as a budget: remaining % colored by state (on_track green,
    # at_risk amber, exhausted red, insufficient gray), each burn
    # rule's pace verdict in the tooltip and a red badge for a rule
    # that is firing. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        "budgetCell",
        "BUDGET_COLOR",
        "at_risk",
        "burning",
        "remaining_pct",
        "on_track",
        "exhausted",
        "burn_rate",
    ):
        assert marker in body, marker


async def test_admin_ui_slo_alert_surface(env):
    # Tier 44: the delivery state — a red "alerting" badge on targets
    # whose alert already fired (fire-once: on-call was paged; the
    # badge stays until the target provably recovers), with the
    # condition and since in its tooltip. Static-shell assertions
    # only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        "alertBadge",
        "alerting",
        "fire-once",
    ):
        assert marker in body, marker


async def test_admin_ui_billing_surface(env):
    # Tier 45: the Billing tab — the month statement (month picker,
    # CSV download, tenants/keys/line-items tables with the budget
    # badge beside the spend). Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'data-tab="billing"',
        'id="tab-billing"',
        "loadInvoices",
        "/admin/invoices",
        "inv-month",
        "btn-invoice-csv",
        "invBudgetCell",
        "shadow mirrors and trial replays excluded",
    ):
        assert marker in body, marker


async def test_admin_ui_finops_surface(env):
    # Tier 46: the Billing tab's finops flags panel — the follow-up
    # question the invoice begs (GET /admin/anomalies): the four flag
    # kinds with colored badges (the human message in the badge
    # tooltip), window/baseline numbers and the ratio column ("—" is
    # an honest undefined, never an invented infinity). The panel
    # hides itself when there are no flags — no flags is the good
    # case — except when detection is off, which gets a visible
    # say-so instead of an absence the operator could mistake for
    # quiet. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="finops-panel"',
        'id="finops-flags"',
        "loadAnomalies",
        "/admin/anomalies",
        "FLAG_BADGE",
        "cost_spike",
        "traffic_spike",
        "traffic_stop",
        "first_seen_model",
        "No flags is the good case",
        "finops.enabled: false",
    ):
        assert marker in body, marker


async def test_admin_ui_forecast_surface(env):
    # Tier 47: the Billing tab's month-end forecast panel — the
    # invoice's forward-looking sibling (GET /admin/forecasts): cards
    # (projected month-end, spend so far, the two daily paces with the
    # trend), tenants/keys tables with projected-vs-budget verdict
    # badges (the recent pace deciding when both projections exist,
    # verdict_based_on in the tooltip) and a complete month rendering
    # its actuals with a "final" say-so. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="forecast-panel"',
        'id="forecast-cards"',
        'id="forecast-tenants"',
        'id="forecast-keys"',
        "loadForecasts",
        "/admin/forecasts",
        "FC_VERDICT",
        "over_budget",
        "at_risk",
        "within_budget",
        "the invoice is the final bill",
    ):
        assert marker in body, marker


async def test_admin_ui_forecast_alerts_surface(env):
    # Tier 48: the forecast panel shows what on-call was paged for —
    # the "paged" badge (fire-once state from the evaluator's
    # settings row) beside the verdict, with the condition and the
    # since-timestamp in the tooltip — and the visible say-so when
    # budget alert delivery is off. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        "fcAlertBadge",
        "on-call was paged",
        ">paged</span>",
        "budget alerts off",
        "f.alerts.enabled",
        "section.alert",
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


async def test_admin_ui_key_cache_policy_surface(env):
    # Tier 54: the key dialog carries the per-key cache policy — the
    # cache select (inherit/bypass) + the window input, submit body +
    # create-mode null-deletion, and the key-row badge. Static-shell
    # assertions only, like the other surface pins.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="k-cache"',
        'id="k-cache-ttl"',
        'cache_enabled: $("#k-cache").value === "off" ? false : null',
        'cache_ttl_seconds: num("k-cache-ttl")',
        "delete body.cache_enabled",
        "delete body.cache_ttl_seconds",
        "cacheBadge",
        "bypass",
    ):
        assert marker in body, marker


async def test_admin_ui_key_last_used_surface(env):
    # Tier 55: the keys table gains the last-used column — the fmtAgo
    # relative-time renderer (just now / Nm / Nh / Nd, calendar date
    # past a month), the "Last used" header, the never-used badge with
    # its rotation-candidate tooltip, the ISO tooltip on real values,
    # and the widened colspan. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        "function fmtAgo(",
        'return "just now"',
        '"m ago"',
        '"h ago"',
        '"d ago"',
        "<th>Last used</th>",
        "lastUsedCell",
        "Never used — a rotation candidate (may also predate tracking)",
        'title="${esc(k.last_used_at)}"',
        'colspan="9"',
    ):
        assert marker in body, marker


async def test_admin_ui_quiet_key_badge_surface(env):
    # Tier 56: active keys idle past the first quiet level (the 7 of
    # alerts.quiet_days 7/30 — same hardcoded-first-level convention as
    # the expiring badge's 14) wear a gray 💤 quiet badge whose tooltip
    # carries the whole policy: what fired, what to do, and that
    # resumed use re-arms silently. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        "quietBadge",
        "idleDays >= 7",
        "💤 quiet",
        "alerts.quiet_days",
        "Resumed use re-arms silently",
        'class="b b-gray" title="Quiet — no use for',
    ):
        assert marker in body, marker


async def test_admin_ui_sso_login_surface(env):
    # Tier 60: the dashboard's SSO login — the probe that reveals the
    # button, the redirect that starts the flow, the ?sso= landing
    # handler with its cookie→Bearer exchange, and the SSO-aware
    # logout. Static-shell assertions only (the flow lives in
    # test_sso.py).
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="btn-sso"',
        'id="sso-or"',
        'id="login-err"',
        "/admin/ui/sso/config",
        "/admin/ui/sso/login",
        "/admin/ui/sso/session",
        "/admin/ui/sso/logout",
        "async function ssoFinish(",
        "async function finishLogin(",
        "history.replaceState",
        'params.get("message")',
        "gw-admin-sso",
    ):
        assert marker in body, marker


async def test_admin_ui_tenant_views_surface(env):
    # Tier 61: the dashboard learns who is signed in — the whoami
    # probe and header line, the role-honest placeholders that
    # replace fired 403s on every platform-only tab, and the
    # per-tenant lenses on keys and logs. Static-shell assertions
    # only (the endpoints live in test_tenants.py / test_sso.py).
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="whoami"',
        "/admin/whoami",
        "const PLATFORM_ONLY",
        "fillTenantSelect",
        'id="keys-f-tenant"',
        'id="f-tenant"',
        "me.tenant.name",
        "me.auth_method",
        "platform_admin",
        "tenant_id",
        # every gated tab names itself with the same honest sentence
        "PLATFORM_ONLY(\"Tenant management\")",
        "PLATFORM_ONLY(\"The prompt library\")",
        "PLATFORM_ONLY(\"Routing, breakers and shadow evidence\")",
        "PLATFORM_ONLY(\"Guardrail configuration\")",
        "PLATFORM_ONLY(\"The playground\")",
        "PLATFORM_ONLY(\"Config reload and database backups\")",
        "is a platform-administrator surface",
    ):
        assert marker in body, marker


async def test_admin_ui_pii_panel_surface(env):
    # Tier 57: the tier-53 teeth get their dashboard surface — a PII
    # scan tab (loading it IS the on-demand scan), the config-echo
    # badges (quarantine/purge/sweep), the honest-disabled 404 branch,
    # the summary cards, and the by-kind + flagged-rows tables.
    # Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'data-tab="pii"',
        'id="tab-pii"',
        "async function loadPII(",
        'id="pii-limit"',
        'id="btn-pii-scan"',
        'id="pii-status"',
        'id="pii-summary"',
        'id="pii-kinds"',
        'id="pii-rows-table"',
        "e.status === 404",
        "Enable pii_scan.enabled",
        "quarantine on",
        "no purge",
        "Bodies not captured",
        "with_findings",
        "bodies_not_captured",
    ):
        assert marker in body, marker


async def test_admin_ui_replay_surface(env):
    # Tier 63: the logs tab carries the replay harness — the hot-reload
    # safety net. The capture (GET /admin/logs/export: the filtered
    # window as JSONL with recorded baselines) downloads or feeds the
    # replay (POST /admin/replay: re-executes each line through the
    # CURRENT request policy — no upstream calls, counters or spend —
    # and diffs against what was recorded); the report dialog shows
    # the per-line results with the changed-field tooltips, capped at
    # 500 rendered rows; a 404 (replay.enabled: false) renders the
    # honest say-so instead of an error toast. Static-shell
    # assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="replay-limit"',
        'id="btn-replay-download"',
        'id="btn-replay"',
        'id="replay-status"',
        'id="dlg-replay"',
        'id="replay-summary"',
        'id="replay-report"',
        "fetchCapture",
        "/admin/logs/export",
        "/admin/replay",
        "REPLAY_BADGE",
        "e.status === 404",
        "replay.enabled",
        "no upstream calls",
        "Showing the first",
    ):
        assert marker in body, marker


async def test_admin_ui_feedback_surface(env):
    # Tier 63: the Feedback tab — client-reported ratings (POST
    # /feedback on the data plane) become reviewable: quality
    # aggregates (GET /admin/feedback/stats: overall cards + by-model /
    # by-key buckets with the 4-5/1-2 sentiment split) and the rows
    # table (GET /admin/feedback) with rating badges, min/max rating
    # and key/model/request-id filters, the platform-admin tenant lens
    # and a pager. Tenant admins see their own rows; the stats query
    # deliberately carries only the scoping filters (no pager or rating
    # bounds — the endpoint takes none). Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'data-tab="feedback"',
        'id="tab-feedback"',
        'id="fb-cards"',
        'id="fb-by-model"',
        'id="fb-by-key"',
        'id="feedback-table"',
        'id="fb-pager"',
        'id="fb-min"',
        'id="fb-max"',
        'id="fb-key"',
        'id="fb-model"',
        'id="fb-reqid"',
        'id="fb-tenant"',
        "loadFeedback",
        "fbStatsFilters",
        "RATING_BADGE",
        "/admin/feedback?",
        "/admin/feedback/stats",
        "avg_rating",
        "pct_positive",
        "pct_negative",
        "POST /feedback",
    ):
        assert marker in body, marker


async def test_admin_ui_live_ops_surface(env):
    # Tier 63: the System tab gains the four platform-only live-state
    # panels — concurrency admission (slots held / waiters queued),
    # the runaway jail (cooldown left, offenses, the Release escape
    # hatch), the canary fleet (outcome badges + Probe now, with its
    # real-quota-cost tooltip) and the MCP proxy (header NAMES only —
    # the values are secrets). A tenant administrator gets the same
    # honest PLATFORM_ONLY sentence, naming each surface; no 403 is
    # fired. Static-shell assertions only.
    body = (await env.client.get("/admin/ui")).text
    for marker in (
        'id="system-concurrency"',
        'id="system-runaway"',
        'id="system-canaries"',
        'id="system-mcp"',
        'id="btn-canary-probe"',
        "refreshConcurrency",
        "refreshRunaway",
        "refreshCanaries",
        "refreshMCP",
        "/admin/concurrency",
        "/admin/runaway/",
        "/admin/canaries/probe",
        "/admin/mcp",
        "data-runaway-release",
        "queued_total",
        "retry_after_seconds",
        "header_names",
        "open_sessions",
        "Costs max_tokens of provider quota per deployment",
        'PLATFORM_ONLY("Concurrency admission control")',
        'PLATFORM_ONLY("The runaway-agent jail")',
        'PLATFORM_ONLY("The canary fleet")',
        'PLATFORM_ONLY("The MCP proxy")',
    ):
        assert marker in body, marker
