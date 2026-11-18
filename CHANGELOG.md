# Changelog

All notable changes to the LLM Gateway are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- Key rotation & grace windows (tier 51) — `POST /admin/keys/{id}/rotate`
  replaces a key's *credential* while the row keeps its identity: name,
  tenant, allowlists, budgets, limits, spend and logs all stay with the
  row, so callers switch secrets without switching identity. The new
  raw key is returned exactly once (like creation); only its hash is
  stored.
  - **Grace window**: `grace_seconds` (default 0) keeps the *previous*
    credential authenticating as the same key until the window ends —
    budgets, limits, spend and logs accrue to the row whichever
    credential was presented. Past the window the old credential
    fails with an explicit 401 ("This API key has been rotated — its
    grace window has ended") instead of a generic unknown-key error;
    an immediate cutover (0) stores the old hash anyway, so its
    holders get the honest message.
  - **The row's checks fire first**: revocation (`DELETE
    /admin/keys/{id}`) and `expires_at` refuse *both* credentials,
    mid-window or not — rotation never resurrects a revoked key or
    moves an expiry deadline. Rotating again supersedes the previous
    credential outright, even mid-window.
  - **The incident knob**: `PATCH /admin/keys/{id}` accepts
    `grace_until` — explicit `null` kills the old credential
    immediately (the old key leaked mid-rotation), a future timestamp
    re-arms or extends its window.
  - **Audited, never leaked**: each rotation emits a `key.rotated`
    audit record (who, which key, the grace window); no record, log or
    public API surface ever carries either raw key or the previous
    key's hash. JWTs keep working across rotation untouched — a token
    names the key, not the credential.
  - **Dashboard**: a Rotate button on every active key (grace prompt →
    the one-time raw-key reveal), and rows in grace show an amber
    badge with the dying prefix and the window's end. 12 tests pin the
    contract (`tests/test_key_rotation.py`).
- Projected budget enforcement (tier 50) — `budget_enforcement:` gives
  the request-time budget gate the leg its hard caps lack. The caps
  (`max_budget_usd`, the daily/monthly windows, key and tenant) are
  enforced always — they are the floor. The new stance is the trend:
  with `enabled: true`, a key or tenant whose month-end **run-rate
  projection** exceeds its `monthly_budget_usd` is denied the same
  `429 insufficient_quota`, the message saying it is the projection
  with the numbers ("projected to spend $10.29 by month end (day
  17.5 of 30), over its $10.00 monthly budget ($6.0000 spent so
  far)") — a key at $8 of a $10 budget on day 2, burning at a
  month-end pace of $120, is told *now*, not two days later with the
  money already gone.
  - **The rules, in order**: the hard cap fires first (a projection
    denial only ever happens with spend still under budget — the two
    messages can never lie about each other); zero spend projects
    zero and passes; a stale bucket is last month's spend and counts
    as 0 (the stance composes with the window rollover);
    `min_elapsed_days` (default 2.0 — the forecast family's honesty
    gate from tiers 47/48) keeps a day-one straight line from denying;
    no budget means no bar, at either level.
  - **It self-heals**: spend over elapsed days is a ratio, so a
    caller that genuinely stops spending watches its projection decay
    toward the actual as the month elapses, falls back under its
    budget, and traffic resumes — no operator action. The hard cap
    stays the wall it always was; the stance is a trend, and trends
    end.
  - **The counter lens, not the billing lens**: the projection reads
    the auth-time `monthly_spend_usd` snapshot the hard cap just read
    (never tier 47's RequestLog measure), so the gate and the cap can
    never disagree about what "spent" means — and the check costs no
    extra query on the hot path (also why it's the straight-line
    run-rate rather than the recent-pace correction: the recent
    window would need a per-request query, and the tier-48
    evaluator's cached verdicts are alert transitions — fire-once
    semantics, the wrong shape for a gate).
  - **One rulebook, two levels**: key and tenant legs share
    `_check_projected_budget`; `_check_budget` gained an injectable
    `now` (the utils' house pattern) so tests pin the month clock;
    the pure math lives in `forecasts.run_rate_month_end` beside its
    billing-lens siblings. Applies on every surface the gate already
    runs — chat (all four inbound formats), realtime (per create),
    embeddings, images, audio; hot-reloads live; **off by default** —
    today's hard-cap-only behavior is exactly preserved until the
    operator opts in.
  - 16 tests in `tests/test_budget_windows.py` (10 → 26): pure units
    (month shapes 28/30/31, the first instant, zero spend, error
    wording at both levels), the gate against a pinned month clock
    (trending key and tenant denied with the numbers, within-budget
    passing, the honesty gate holding a would-be $120 projection,
    stale bucket zero, hard-cap-first, the self-heal decay), and the
    traffic paths (off-by-default at the gate and through HTTP, an
    adaptive HTTP denial computed from the test's own clock that
    holds on any day of any month, the honesty gate through HTTP, no
    budget, stale bucket, hard-cap precedence).
- MCP guardrail coverage (tier 49) — the three audit gaps closed: the
  proxy's guardrails now cover every content-bearing method instead of
  `tools/call` alone.
  - **The content methods guard both directions.**
    `resources/read`, `prompts/get` and `completion/complete` now run
    the request-phase guardrails over the whole serialized `params`
    (uri, name, `arguments`, `ref`, the completion `argument` —
    everything the caller controls), and the response phase over every
    server text shape they can answer with: `contents[].text`, prompt
    message text blocks (in **both** content shapes — a single block or
    a list, because the spec widened mid-life and servers ship both),
    and `completion.values[]`. A modify verdict applies only while the
    rewrite stays a JSON object — the arguments rule, now on params
    too; redirects and corrupt rewrites fail closed; denials stay
    in-band JSON-RPC errors (HTTP 200), nothing upstream sees them.
  - **`tools/call` results cover the non-text shapes.** The response
    phase now scans `structuredContent` (as serialized JSON, a rewrite
    applied only while it stays a JSON object — corrupt rewrites fail
    closed) and the **text of embedded-resource items**. Base64
    payloads — image/audio items, resource `blob`s — are binary, with
    no text to scan: skipped by design and passed through
    byte-identical.
  - **Everything else stays verbatim by design**: lifecycle
    (`initialize`), notifications, catalog reads (`tools/list`,
    `resources/list`, `prompts/list`), `ping` — nothing the caller
    wrote beyond a cursor, nothing the server generated beyond static
    descriptions.

  Guardrail **`scope:` binds on this route as on every other**, now
  pinned by tests: `routes: ["mcp"]` is the lever for MCP-only
  policies (the same content denied here passes on chat), `models:`
  matches the **server name** (the log row's model column — an
  in-scope guardrail governs the server's traffic while an
  other-server scope never runs, and chat model names never collide
  with server names), and `keys`/`exclude_keys` match the calling key's
  name. `MCPToolBlocked` gained the denied `method`, so the
  `mcp.tool.blocked` event (and the blocked log row's context) says
  whether the denial was a `tools/call`, a `resources/read`, a
  `prompts/get` or a `completion/complete`.

  Internally the pass is factored into three shared helpers
  (`_mcp_guard_request` / `_mcp_guard_response_text` /
  `_mcp_guard_result_items`) that `tools/call` and the new
  `_mcp_content_call` both use — one rulebook, two dispatch paths, no
  drift. 18 tests in `tests/test_mcp.py`; the suite stands at 1259.
- Budget forecast alerts (tier 48) — the tier-47 projections get
  teeth. A forecast nobody reads is a spreadsheet, so a background
  evaluator (the canary-loop shape: always started, re-reading
  `forecast_alerts:` each pass; one pass = the same `forecast()` the
  admin endpoint serves, so an alert is decided by exactly the
  numbers the dashboard shows) turns the month-end projections into
  the two transitions on-call consumes:
  - **`forecast.alert`** when a budgeted entity starts trending badly —
    the recent projection puts it over its `monthly_budget_usd`
    (`trending_over`) or at risk ≥80% (`trending_at_risk`). Both levels
    alert: a key against its own budget, a tenant against the pooled
    one. Paging on where the month is *going* — actual spend crossings
    stay the tier-2 threshold alerts' job.
  - **`forecast.recovered`** when the projection is provably back within
    plan — often because the operator raised the budget: an honest fix
    recovers honestly, with the healed numbers in the payload.

  Fire-once / re-arm per **(month, entity)**, persisted on the settings
  KV table (`{month}|{kind}:{id}` — a restart never re-pages, and a
  new month is a new plan: December trends get paged even if
  November already paged); recovery is what re-arms within a month.
  The honesty rules: **`min_elapsed_days`** (default 2) gates the
  early month — a day-one straight-line extrapolation is hours of
  evidence dressed as a trend and never reaches a webhook — and **no
  budget means no bar**: sections without one never fire, and a
  budget removed while firing drops the state silently (nothing to
  recover to; a re-added budget starts cold). Delivery is best-effort
  (bounded retries, `gateway_alerts_total{kind="forecast",
  outcome=sent|failed|skipped}`); without a webhook URL the POST is a
  skipped no-op while the observer events still fire;
  `forecast_alerts.enabled: false` is the kill switch — no state, no
  events — while the forecast keeps serving.

  `GET /admin/forecasts` now attaches each section's persisted alert
  state (`alert`: firing, condition, since — what on-call was already
  paged for, `null` when never fired or recovered) and echoes the
  `forecast_alerts:` delivery config. The dashboard's forecast panel
  shows a red "paged" badge beside the verdict while firing (condition
  + since in the tooltip) and a "budget alerts off" say-so in the
  period line when delivery is disabled. `parse_month` gained an
  optional clock pin so the evaluator's tests (and any future
  caller) can fix the month under evaluation; production callers are
  unchanged. 11 tests + a UI pin in `tests/test_forecast_alerts.py`
  / `tests/test_admin_ui.py`; the suite stands at 1241.
- Spend forecasts (tier 47) — the invoice's forward-looking sibling:
  where will this month land? `GET /admin/forecasts` (`month=YYYY-MM`,
  the invoice's own month contract) projects the final bill per key and
  per tenant over the same client-consumed lens, with two projections
  because one honest number is not always possible:
  - **Run-rate** — spend so far over the elapsed fraction of the month,
    scaled to the whole month. Straight-line and honest about being an
    average: a key that stopped spending three days ago still projects
    its old pace.
  - **Recent pace** — the last 7 days' daily spend (bounded to the
    month: on day 2, "the last 7 days" is really 2 days, and divides
    by 2) carried over the remaining days on top of the booked spend.
    The correction: a stopped key projects flat, an accelerated one
    projects the acceleration.
  - **Trend ratio and verdicts** — the recent daily pace over the
    month-to-date daily average rides beside both, and the
    projected-vs-budget verdicts (`over_budget` / `at_risk` ≥80% /
    `within_budget`) are decided by the recent-pace projection when
    one exists, `verdict_based_on` saying which. Both projections
    always ship side by side — a verdict is never a mystery about
    its inputs.

  The honesty rules, forward-looking edition: a **complete month is not
  forecast** — a past month serves its actual total as the projection
  (`month_complete: true`, remaining 0, trend null; the invoice *is*
  the final bill), and a future or malformed month is the invoice's
  400. A **trend needs 2+ days of month** and a nonzero month average —
  less is hours of evidence dressed as a line, so `trend_ratio` is null
  there, and a zero average with recent spend is null too, never a
  fabricated infinity. The **clock context ships in the envelope**
  (`elapsed_days` / `remaining_days` / `days_in_month`), so every
  projection is recomputable from the response itself. **No budget →
  projected pct and verdict null**, while every existing key still
  appears — an unused key projects $0 of its budget; deleted keys
  project where they lived, no budget to compare against. Read at
  request time — no loop, no state, no config (the 7-day window is a
  module constant). Tenant admins see their own tenant only (forced);
  platform admins may `?tenant_id=` filter.

  The Billing tab grows a Month-end forecast panel between the
  statement and the flags — where will it land, then is any of it
  unusual — both projections in one cell with the other a tooltip
  away, verdict badges at the 80/100% thresholds, and a complete month
  rendering its actuals with a "final" say-so.
  9 tests + a UI pin in `tests/test_forecasts.py` /
  `tests/test_admin_ui.py`; the suite stands at 1229.
- Cost anomaly flags (tier 46) — the invoice's follow-up: is any of
  this unusual? `GET /admin/anomalies` compares the recent window
  (`finops.window_hours`, default 24h) against each key's trailing
  baseline (`finops.baseline_days`, default the 14d immediately
  before it), per key and per model over the same client-consumed
  lens the invoice bills. Four flag shapes, each a billing surprise
  in its own way:
  - **`cost_spike` / `traffic_spike`** — the window's daily rate at
    ≥ `finops.spike_ratio` (default 3×) the baseline's: the
    runaway-agent shape, the someone-pointed-a-loop-at-us shape;
    volume before it becomes money.
  - **`traffic_stop`** — a key with real baseline traffic that went
    silent: the churn / broken-integration signal a spend dashboard
    structurally cannot show (nothing is a great price).
  - **`first_seen_model`** — a model with zero baseline traffic
    appeared in the window: something new is on the bill, and new
    line items are how invoices surprise people.

  The honesty rules (the SLO epic's, money edition): a thin baseline
  (< `finops.min_baseline_requests` rows, default 10) is not a
  verdict — no spike and no silence on thin evidence, and a
  brand-new key cannot spike at all (it appears on the invoice, not
  here with a fabricated verdict); a zero-cost baseline with window
  spend fires with `ratio: null` and a message saying the baseline
  cost nothing — never a fabricated infinity. Every flag carries its
  own window/baseline numbers and a human message, the window and
  baseline bounds in the envelope. Informational only — no loop, no
  state, no delivery (error-rate health is the SLO epic's job; this
  is the money lens); `finops.enabled: false` serves an honest empty
  flag list while the invoice keeps serving. Tenant admins see their
  own tenant only (forced); platform admins may `?tenant_id=` filter
  or read the whole platform.

  The Billing tab grows a Finops flags panel beside the statement
  (colored kind badges with the human message in the tooltip, the
  window/baseline numbers beside the ratio where one exists; the
  panel hides itself when there are no flags — no flags is the good
  case — except when detection is off, which gets a visible say-so).
  8 tests + a UI pin in `tests/test_finops.py` /
  `tests/test_admin_ui.py`; the suite stands at 1219.
- Usage invoices (tier 45) — the billing epic's first surface. The
  usage export is chargeback raw material (one row per request);
  `GET /admin/invoices` answers the finance-side question: what did
  each tenant consume this month, per key, per model? The same
  request-log rows rolled up into a month statement, with the key's
  and tenant's `monthly_budget_usd` beside the spend — so every
  statement doubles as a spend-vs-plan check.
  - **The billing lens is client-consumed usage.** Shadow mirrors
    (never cost the tenant anything) and candidate-trial replays
    (deliberately re-run traffic) are excluded; cache hits are
    included — a cached answer is a real served request, already
    billed at the cached rate the calculator recorded.
  - **Three levels, sorted by spend everywhere.** Line items per
    (key, model) with requests, errors, the token splits and cost;
    keys into tenants (each with `budget_pct` against its own
    monthly budget and the tenant's pooled one, honestly `null`
    when no budget is set); platform keys in a separate
    `unassigned` bucket; exact totals over everything.
  - **The finops findings ship too.** Zero-spend keys still appear
    (an unused key is a finding, not an absence), and keys whose
    row is gone keep their line items under the tenant they lived
    in, with no budget to compare against — log retention
    outliving the key row is the normal case.
  - **Calendar months in UTC**, `month=YYYY-MM` (default the
    current month); a malformed or future month is the app's 400
    envelope — an invoice for a period that has not happened must
    not pretend to be empty.
  - **Scoping:** tenant admins invoice their own tenant only
    (forced, even when they ask for another), platform admins may
    filter `?tenant_id=`, plain keys get the admin 403.
    `format=csv` is one row per line item in a stable 13-column
    order, an attachment named `invoice_<month>.csv`.
  The dashboard grows a Billing tab (month picker, CSV download,
  totals cards, tenants/keys/line-items tables with the budget
  badge colored at the 80/100% thresholds). 9 tests + a UI pin in
  `tests/test_billing.py` / `tests/test_admin_ui.py`; the suite
  stands at 1210.
- SLO alert delivery (tier 44) — the tier-42/43 verdicts get teeth.
  Measurement pages no one; delivery does. A background evaluator
  (the canary-loop shape: always started, re-reading `slos:` each
  pass, so hot-reload applies from the next pass) runs one verdict
  pass per `slos.evaluation_interval_seconds` and turns it into the
  two transitions an on-call consumes.
  - **Decided by the served numbers.** Each pass is exactly the
    `status()` behind `GET /admin/slos` — an alert can never drift
    from what the dashboard shows.
  - **Fire-once / re-arm, persisted.** `slo.alert` when a target
    starts firing — bar `breached`, a burn rule `burning`, or the
    budget `exhausted`, in that precedence — and `slo.recovered`
    when it is provably healthy again. State lives on the settings
    KV table (a restart never re-pages), persists before delivery
    (a crash mid-pass prefers duplicate silence over duplicate
    noise), and only a genuine recovery re-arms.
  - **A quiet window is not a verdict — in either direction.**
    Insufficient evidence holds the previous state: an SLO that
    stops receiving traffic neither fires nor quietly un-fires.
  - **Best-effort delivery that never blocks.** Webhook payloads
    carry the definition, the condition, the numbers and the human
    message; bounded retries; outcomes counted on
    `gateway_alerts_total{kind="slo",outcome=sent|failed|skipped}`.
    No URL: skipped no-op while the events still fire.
    `slos.alerts.enabled: false`: the whole evaluator off — no
    state, no events — while measurement keeps serving.
  `GET /admin/slos` rows carry the target's persisted `alert` state
  (what on-call was already paged for), and the Overview panel
  marks fired targets with a red *alerting* badge until they
  provably recover. 10 tests + a UI-surface pin; the suite stands
  at 1200.
- Error budgets & burn rate (tier 43) — the tier-42 SLO model gains
  its second lens. A bar says whether the promise held; a budget
  says how fast it is being spent. Every metric is restated as a
  bad-event fraction against its allowance: `error_rate` 0.01
  allows 1% failed+blocked rows (the bar IS the allowance), and
  `latency_p95_ms` 800 implies 5% of requests may exceed 800ms —
  the percentile and the tail allowance are one promise seen from
  two sides, computed over the same rows so the budget and the
  verdict can never disagree about what counts.
  - **Burn rate & remaining.** burn = observed / allowed, budget
    remaining = 1 − burn (negative when the window overspent).
    States: `on_track` (burn ≤ 1), `at_risk` (within allowance
    overall but a burn rule is firing — fine on average, spending
    fast NOW), `exhausted` (overspent), `insufficient_evidence`
    (thin window — the same honesty the bar verdict has).
  - **Multiwindow burn rules (the SRE recipe).** `slos.burn` —
    defaults are the workbook's canonical pairs: 14.4× over 1h
    confirmed by 5m (page), 3× over 1d confirmed by 2h (ticket).
    The long window establishes the pace is real; the short one
    confirms it is STILL burning — the two-window AND is the
    anti-flap, so a burst that already ended reads `steady` and
    never pages. Every window carries its own sufficiency gate,
    `min_requests` scaled by the window's share of the target's
    own window and floored at 1. `burn: []` disables the pace
    verdicts.
  - **Zero-tolerance bars never invent an infinity.** A `target: 0`
    allowance has no rate to compute: any bad event reads
    `exhausted`, the burn rules are `insufficient_evidence` with
    the reason saying so, and burn/remaining stay `null`.
  The `GET /admin/slos` rows grow a `budget` and a `burn` block
  beside the tier-42 verdict (numbers still beside every verdict,
  still read-at-request-time), and the Overview panel grows a
  Budget column: remaining % colored by state, every burn rule's
  pace verdict in its tooltip, red badges for rules that are
  firing. 13 tests + a UI-surface pin; the suite stands at 1189.
- Service level objectives (tier 42) — named promises about the
  client experience, measured and verdicted honestly. A latency bar
  in a design doc is a hope; the same bar verdicted against the
  request logs is an SLO. `slos:` in config.yaml declares targets —
  a metric (`error_rate`, `latency_p50_ms`/`p95`/`p99`, `ttft_p95_ms`,
  all lower-is-better ceilings), a `target` bar, a rolling
  `window_minutes` and a `min_requests` sufficiency minimum, scoped
  to a `model` (exact public name) or narrowed to one `deployment` —
  and `GET /admin/slos` (platform-admin-only) measures each and
  verdicts it.
  - **Client experience, not deployment health.** Every non-shadow,
    non-trial row for the scope: cache replays INCLUDED (a cached
    answer is a fast answer the client really received) and, at
    model scope, the unattributed error rows too — the client
    experienced the failure even when no dispatch completed to
    blame a deployment (the tier-40 attribution story). A
    deployment-scoped target measures only its attributed rows,
    exactly like the post-promotion watch.
  - **Honest verdicts.** `met` (the ceiling holds), `breached` (the
    `reason` names the metric, the number and the bar), or
    `insufficient_evidence` — fewer than `min_requests` requests in
    the window (a quiet window is not a verdict), or nothing
    streamed for a TTFT target (an unmeasurable metric is no
    answer, never a fabricated 0).
  - **The numbers beside every verdict.** Each row ships the full
    measurement — requests, errors, error rate, latency p50/p95/p99,
    TTFT p95 — all from one pass over the same rows, so a verdict is
    auditable and re-bucketable, never taken on faith.
  - **Pure config, measured at read time.** No loop, no cached
    verdicts, no notifications in this tier — a tightened bar
    re-verdicts on the next read, hot-reload applies instantly.
    The surface is the measurement and the honest answer; alert
    delivery on breach is the next tier.
  The admin dashboard's Overview tab gains an SLO panel above the
  traffic tables: objective, scope, metric, the bar, the measured
  value, the request count against the `min_requests` gate, and the
  met/breached/insufficient badge (the failed reason in its tooltip)
  — hidden when no targets are configured. 7 tests + a UI-surface
  pin (`tests/test_slos.py`, `tests/test_admin_ui.py`); the suite
  stands at 1175.
- Admin playground (tier 41) — the dashboard's own chat client.
  "What does a request from this key actually experience?" now has
  an answer that IS the thing, not a simulation of it:
  `POST /admin/playground/chat` (platform-admin-only) runs one chat
  completion through the **real pipeline** and returns the
  client-facing completion verbatim beside the diagnostics of what
  happened while producing it.
  - **Impersonation, not simulation.** The body's optional `key`
    names a virtual key (the human label from `/admin/keys`),
    resolved through the same chain the JWT auth step uses — name →
    active/expiry checks → tenant → the `auth_context_from_key`
    snapshot — so the request carries that key's full policy
    reality: model/tool allowlists, budgets, residency, guardrail
    scopes, rate limits, and **billing** (spend, log rows and usage
    exports accrue to the key exactly as live traffic would).
    Omitted, the calling platform admin's own (unrestricted)
    context runs the chat. In both cases every row and audit event
    carries `auth_method: "playground"` — the one thing separating
    playground traffic from the key's live traffic.
  - **The real pipeline, unchanged.** Same `route="chat"` surface as
    `/v1/chat/completions` (guardrail route-scopes behave
    identically), same guardrails, router, retries, response cache
    (an identical repeat hits it — and says so) and accounting.
    Errors pass through verbatim — a guardrail block, exhausted
    budget, rate-limit refusal or upstream failure surfaces as the
    exact envelope a real client would receive. Non-streaming only:
    `stream: true` is an explicit 400.
  - **The diagnostics are the request-log row the run just wrote** —
    the same one `/admin/logs` shows and the usage export bills:
    request id, key, model/deployment/provider, status (including
    `blocked`), tokens, cost, latency, cache-hit and the
    `guardrail_actions` verdicts. One honest source.
  - **The trail is the price of the grant.** Every attempt —
    success, blocked or failed — is audited as `playground.chat`
    with the calling admin as the actor and the impersonated key,
    model, request id and outcome in the details.
  The admin dashboard gains a Playground tab: model dropdown (from
  `/admin/routing`), key dropdown (from `/admin/keys`, empty =
  admin), max-tokens/temperature knobs, a client-side conversation
  where each Send posts the whole history (a real multi-turn body),
  and the diagnostics panel — errors render there as the envelope
  the real client would see. 12 tests + a UI-surface pin
  (`tests/test_playground.py`, `tests/test_admin_ui.py`); the suite
  stands at 1167.
- Post-promotion watch (tier 40) — the migration story continued
  after the promotion flips the candidate live. Promotion ended the
  mirrors: the candidate serves, the pair-shaped evidence stops, and
  the operator is back to watching by feel — exactly when watching
  matters most. `GET /admin/migration/watch` (platform-admin-only)
  re-verdicts **what the logs still say**: one row per serving
  deployment, judged by the same `migration:` thresholds with the
  same go / hold / no_go / insufficient_evidence vocabulary the
  pre-promotion verdicts used:
  - **The evidence is the attributed serving rows** — the
    client-facing rows logged once a dispatch *completed* on the
    deployment, with mirrors (shadow twins and prompt A/B),
    candidate-trial replays and cache replays excluded: evidence,
    experiments and cache hits are not serving traffic.
  - **The attribution is the honest part, and it cuts both ways.**
    A request that fails *before* any dispatch completes (the
    router exhausted its ladder — a sole deployment failing every
    attempt, or a candidate rescued by a tier peer) logs its row
    with no deployment, so a failing candidate shows up as **traffic
    that ran away**, never as fabricated attributed errors. The
    row's share split says it loudest: `expected_share` (its
    weight's share of its priority tier) vs `actual_share` (its
    fraction of the tier's attributed requests in the window). The
    error rows that *do* land on a deployment are failures after
    the dispatch completed — a stream that dies mid-flight — and
    those feed the error-rate check.
  - **The window is measured on the traffic itself.** Each row
    watches from `since` — the deployment's first serving row, the
    promotion moment as traffic saw it; no side state, nothing to
    drift. The baseline aggregates the same-priority-tier peers
    over exactly that window (same traffic regime, same clock).
    `?since=` (ISO-8601) overrides every window; `?model=` /
    `?deployment=` narrow; unknown names are empty data.
    Thresholds are echoed live.
  - **The checks, same shape as every verdict:** `requests`
    (the `min_pairs` sufficiency gate), `error_rate` (against
    `max_shadow_error_rate` — the same SLO the mirror verdicts
    judged by, measured on serving traffic) and
    `latency_delta_pct` (against `max_latency_delta_pct` — null
    and skipped with a caveat when unmeasurable, never a fake 0%).
    A `no_go` outranks a `hold`; thin volume is
    `insufficient_evidence` regardless of the numbers.
  The verdict is a **recommendation to keep serving or roll back** —
  the endpoint never pushes the rollback button itself. The admin
  dashboard's Routing tab carries a Post-promotion watch table:
  per deployment the go/no-go badge (failed-check reasons in the
  tooltip), the expected → actual share split, requests/errors,
  latency mean, the latency delta vs the tier, cost mean, and the
  watching-since column. 11 tests + a UI-surface pin
  (`tests/test_watch.py`, `tests/test_admin_ui.py`); the suite
  stands at 1154.
- Promotion rollback (tier 39) — promotion's undo, the same
  discipline in reverse. Tier 38 left an operator staring at a
  promoted canary with a timestamped backup file and no API that
  knew about it: rolling back meant stopping the gateway, swapping
  files by hand, hoping the operator picked the right `.bak`. Now
  the backups promote (and rollback) writes ARE the undo surface:
  - **`GET /admin/migration/backups`** — list, newest first (the
    UTC stamp in the name sorts chronologically — name order IS
    time order, no filesystem clock), a pure read. Each row
    *explains* its backup: `created_at` (the stamp), `size_bytes`,
    whether it still `parses` (an old backup can predate schema
    changes — honestly labeled, refused at apply time),
    `is_current` (byte-identical to the live file), and the
    structured `changes` restoring it would apply, computed by
    `core/promotion.compare_configs` over the parsed pair:
    `shadow`/`unshadow` (a promotion's flip, undone — the candidate
    goes back to mirroring), `set_weight`/`set_priority` with
    `from`/`to`, `add_deployment`/`remove_deployment` (a
    cross-model insert, undone).
  - **`GET /admin/migration/backups/{name}`** — the row plus the
    line-level `diff` of the live file → the backup's text: the
    human preview. Flat names only — the charset bars path
    separators, so neither endpoint can ever reach outside the
    config file's directory.
  - **`POST /admin/migration/rollback`** — apply, the same
    discipline in reverse. `backup` names the file; omitted = the
    latest, the common "undo the last promotion". The chosen backup
    **must parse** (409 `backup_invalid` — never start an edit that
    cannot land); a byte-identical one is refused as the no-op it
    is (409 `rollback_noop`); the live file is backed up **first**
    (the rollback's own undo point), then the backup's text is
    written, the same hot-reload path `POST /admin/reload` takes
    applies (source `rollback`), and the act is audited as
    `promotion.rolled_back` with the structured changes. On reload
    refusal the undo point is restored and the running config never
    moved — the same safety net promote has.
  - **A rollback is one rollback away from undone, in both
    directions.** After a rollback, the latest backup is the
    rollback's own undo point — the promoted state — so rolling
    back again with no body *redoes* the promotion (pinned by a
    test). No evidence gate, no force flag: rollback IS the escape
    hatch — but a deliberate, audited one, never automatic.
  The admin dashboard's Routing tab gains a Backups button beside
  the shadow-diff table: one row per backup with its changes story
  (does-not-parse and no-op rows carry honest badges instead of a
  rollback button), a Preview of the line-level diff, and a
  confirm-guarded Rollback-to-this that toasts the undo point and
  refreshes the routing tables. All three endpoints are
  platform-admin-only. 9 tests + a UI-surface pin
  (`tests/test_rollback.py`, `tests/test_admin_ui.py`); the suite
  stands at 1142.
- Migration promotion (tier 38) — the deliberate act that turns a go
  verdict into serving traffic, through config.yaml itself. Tier 37
  ended at the recommendation; the operator reading "go" still had to
  edit config.yaml by hand — drop the `shadow:` lines, pick a weight,
  align the priority tier, reload, and hope no typo took the gateway
  down mid-edit. Now that edit is a two-step API, and config.yaml
  stays the single routing truth (no DB overlay, no side state — the
  file, the reload and the audit trail all tell the same story):
  - **`POST /admin/migration/plan` — preview, no side effects.**
    Renders the exact line-level config.yaml edits for promoting a
    `deployment` into a `model` at `weight` (1–100, default 10), plus
    the evidence verdict justifying them. The response carries the
    promotion `mode` (`shadow_flip` — a shadow twin flips to serving;
    `rebalance` — an already-serving deployment's weight changes;
    `cross_model_insert` — a candidate on another model is copied
    into the target), a structured `changes` list, a unified `diff` of
    the file, the resulting `canary_share` of the serving tier's
    weight, `warnings`, and the `recommendation` with its
    `evidence_source` (the standing mirror group, else the latest
    completed trial for the pair, else none — thin evidence honestly
    labeled). The file is read, never written; nothing is audited.
  - **`POST /admin/migration/promote` — apply.** Re-builds the plan
    from the file *at apply time* — a preview is never applied stale
    — gates on the verdict, then: timestamped backup → write → the
    same hot-reload path `POST /admin/reload` takes → audit
    `promotion.applied` (model, deployment, weight, mode, verdict,
    evidence source, force, backup, changes). If the reload refuses,
    the backup is restored, the running config never moved, and the
    operator gets a 400 saying exactly that.
  - **Canary semantics, nothing auto-removed.** A flipped candidate
    serves inside the incumbent's priority tier — the canary ladder a
    manual edit would climb: promote again at a higher weight to
    shift more traffic. The incumbent is never demoted or removed; a
    cross-model insert is a copy, its original untouched; a
    rebalance to the weight a deployment already has is an honest
    no-op (empty changes, byte-identical file).
  - **The gate.** A `no_go` verdict is a 409 `promotion_blocked`
    with the reasons in the message, unless `force: true` — a forced
    promotion applies and is audited as forced. A `hold` or
    `insufficient_evidence` verdict does not block; the concern
    rides along as a warning on the promotion.
  - **Honestly bounded text surgery.** The edits are line-level over
    config.yaml's canonical block form only (dash line + indented
    `key: value` lines): comments between entries survive
    byte-for-byte, CRLF files stay CRLF, and anything the surgery
    cannot edit exactly is refused with a clear 409 — flow-style
    entries (`yaml_flow_style`) and a file that disagrees with the
    running config (`yaml_mismatch`) — never best-effort-edited.
  The admin dashboard's Routing tab puts a "Promote…" button on every
  shadow-diff row, opening a dialog that re-plans on every weight
  change (the diff *is* the preview) and applies with the same
  backup → reload → audit flow; a blocked promotion stays in the
  dialog so force is one checkbox away. Both endpoints are
  platform-admin-only. 16 tests + a UI-surface pin
  (`tests/test_promotion.py`, `tests/test_admin_ui.py`); the suite
  stands at 1132.
- Migration verdicts (tier 37) — the go/no-go recommendation engine
  over the migration evidence. Tiers 13/35/36 collected the evidence:
  mirrored pairs scored raw-vs-raw, candidate trials replayed on
  demand. What was still missing was the *call* — an operator
  staring at "match rate 96%, mean similarity 0.91, 2.4% shadow
  errors, +18% latency" had to judge by feel whether the candidate
  is promotable. Now the evidence becomes a recommendation,
  auditable and never authoritative:
  - **One engine, both evidence legs.** `core/migration.evaluate`
    is a pure function over one evidence group — the exact shape a
    shadow-diff mirror group and a trial run's summary both carry —
    so `GET /admin/migration/verdicts` (mirrors, `?model=`/
    `?deployment=`/`?since=`) and the recommendations folded into
    every `/admin/trials` row and detail verdict identically
    through one code path. No DB, no clock, no state to drift.
  - **The vocabulary.** `go` — every bar cleared. `hold` — answers
    fine, a soft concern needs a business call (a marginal
    similarity mean, or a latency regression measured as a
    *percent of the primary's mean*, because an absolute bar means
    different things on a 200ms and a 20s model). `no_go` — answers
    diverge, or the candidate fails the traffic it is given.
    `insufficient_evidence` — too thin to judge. The worst honest
    call wins; a hold never outranks a no_go.
  - **Sufficiency gates both volumes.** `min_pairs` (20) applies to
    total pairs (the volume a shadow-error rate is measured on)
    *and* comparable pairs (the volume answer equivalence is judged
    on — not_comparable rows prove nothing about it), with one
    deliberate exception, pinned by a test: a shadow-error rate
    over its bar on a full volume of pairs is a no_go even when
    nothing was comparable — a candidate that fails every call it
    is given has already answered the question.
  - **Nothing is faked.** Unmeasurable checks (missing latency
    means, a zero primary mean) report null and are skipped, never
    passed; every verdict ships its per-check numbers (`value` +
    `threshold` + `pass`) and one human reason per failed check, so
    a recommendation can be audited, re-bucketed or second-guessed
    — never taken on faith. All five thresholds live in the new
    `migration:` config section, read live (tighten a bar, reload,
    and the next read re-verdicts) and echoed in every response.
  - **Fixed on the way through:** mirror log rows recorded ~0ms
    latency — both mirror tasks built the row's context *after*
    the upstream call, so the logged `latency_ms` measured from
    the context's birth, and every shadow-diff "Δ latency" read as
    an implausible speedup. Shadow and prompt-A/B mirror rows now
    stamp the call's actual start (the same `started_monotonic`
    stance trials had from the start), so the latency leg of the
    evidence — and tier 35's delta column — is real.
  The endpoint is platform-admin-only like the evidence it reads;
  the admin dashboard's Routing tab renders a "Go / no-go" badge on
  each shadow-diff row and each trial run (failed-check reasons in
  the tooltip). 17 tests + a UI-surface pin (`tests/test_migration.py`,
  `tests/test_admin_ui.py`); the suite stands at 1115.
- Candidate trials (tier 36) — the deliberate-replay leg of provider
  migration. Shadow mirroring + diffing is passive evidence: only the
  traffic a model actually gets, only candidates wired as `shadow:
  true`, and only at the rate real requests arrive. When a migration
  decision needs evidence now — or the candidate isn't wired as a
  shadow — `POST /admin/trials` replays a captured traffic window on
  demand: one public model's successful, body-storing chat rows
  (`since`/`until`, newest first, up to `limit`) are re-sent to a
  named candidate deployment with REAL upstream calls (unlike the
  dry-run replay harness, which exists to diff config, not providers):
  - **The candidate can be any configured deployment** — it does not
    need to belong to the replayed model. The adapter maps the
    request to the candidate's upstream model, which is exactly how
    migrations work (a candidate usually runs a *different* model).
  - **Throttled and spend-capped** — entries replay in waves of at
    most `concurrency` parallel calls (the batch worker's wave
    precedent), and between waves the runner re-reads the run row:
    cumulative candidate spend at `max_cost_usd` refuses the NEXT
    wave (never one in flight — the cap can overshoot by at most one
    wave), a status change stops the run. All three per-run values
    are clamped to the new `trials:` ceilings (`max_entries` 200,
    `max_concurrency` 2, `max_cost_usd` $1.00; asking for more just
    runs at the ceiling — the clamped params are what the row
    stores and what ran).
  - **Mirror-like execution, minus the breaker** — trial calls skip
    guardrails, rate limits, budgets, the caches, lifecycle events
    and metrics, and unlike real mirrors they skip *breaker
    bookkeeping* too: an experiment must not trip a serving
    candidate's circuit. Nobody's spend moves — each replayed entry
    writes a shadow request-log row sharing the original's
    `request_id` (plus a `trial_id`, auto-upgraded column) with its
    own cost, totaled on the run row, billed to no key or tenant.
    The run row plus the scored log rows are the evidence surface.
  - **Scored like mirrors, honestly** — every replayed entry is
    scored with the tier-35 verdict vocabulary against the
    primary's stored body. One difference is forced by the
    persistence boundary: the primary's raw provider text is not
    stored, so a primary that a response/stream guardrail REWROTE
    (its stored body is the client-seen text) scores
    `not_comparable` instead of fake divergence — the same
    raw-vs-raw honesty mirrors get by capturing the baseline before
    the response phase, paid for here with one bucket.
  - **The standing evidence stays clean** — trial rows are excluded
    from `/admin/shadow-diff` (`trial_id IS NULL` is the live-
    mirroring scope); `GET /admin/trials/{id}/pairs` is the run's
    own drilldown, the same pair shape, verdict-filterable
    (`?verdict=divergent` = the trial's review queue). `GET
    /admin/trials` lists runs newest-first with progress
    (`selected`/`completed`/`skipped`), spend, stop reason and the
    verdict summary folded into each row (one grouped query — no
    N+1); `GET /admin/trials/{id}` is the run plus its summary.
  - **DB status is the only truth** — claiming (`pending` →
    `running`), progress/cost accrual (column-arithmetic UPDATEs)
    and finalization are guarded on the row's current status, and
    the runner re-checks between waves: `POST /admin/trials/{id}/
    cancel` (finished runs answer 409), another replica, or the
    startup sweep — trials a dead process left `pending`/`running`
    are cancelled with `orphaned_by_restart` at boot; a run live on
    another replica sees the cancel within one wave — always win.
    Stop reasons say why a run ended: `done` / `no_entries` /
    `cost_cap` / `cancelled` / `orphaned_by_restart` / `error`.
  All five endpoints are platform-admin-only (the pairs carry
  request bodies, which cross tenants — the same boundary as
  `/admin/shadow-diff`); create/cancel are audited
  (`trial.created` / `trial.cancelled`). `trials.enabled: false`
  answers the POST with 404 `trials_disabled`. The admin
  dashboard's Routing tab gains the "Candidate trials" panel — run
  form (model window, candidate, caps), runs table with verdict
  badges, and the same pair-dialog as mirror pairs, endpoint-swapped.
  15 tests + a UI-surface pin (`tests/test_trials.py`,
  `tests/test_admin_ui.py`); the suite stands at 1097.
- Shadow diffing (tier 35) — the evidence layer over mirrored traffic.
  Shadow mirroring already wrote paired rows (the client-facing primary
  and the mirrored candidate share a request_id, each with its own
  body, latency, tokens and cost), but nothing ever compared them: an
  operator weighing a provider migration had hundreds of pairs and no
  way to answer "how divergent is the candidate? at what latency/cost
  delta? show me the worst pairs." Now every mirror is scored at
  mirror time:
  - **Raw-vs-raw** — the mirror's answer is diffed against the
    primary's RAW provider text: extracted before the response phase
    on the buffered path (an immutable string, so no rewrite can leak
    into the baseline), the accumulated source text on the streaming
    path (captured by value at queue time, so no scheduling race
    either). Mirrors run no guardrails by design, so a primary-leg
    rewrite never reads as provider drift — the PII redactor can mask
    every client-seen body and the pair still scores exact.
  - **Fixed verdict buckets, no config surface** — `exact`
    (difflib ratio 1.0), `near` (≥ 0.80, cosmetic drift), `divergent`
    (different answers), `shadow_error` (the mirror call itself
    failed — a candidate that can't serve traffic is the loudest
    finding of all), `not_comparable` (no text on a side: tool-call
    replies, empty content — never a fake 0.0 that would read as
    maximal divergence). The raw similarity ships on every row
    (`RequestLog.shadow_similarity`, visible in `/admin/logs`) so
    re-bucketing with different thresholds stays possible.
  - **Aggregation stays SQL** — verdict + similarity are computed in
    the mirror task and written onto the shadow row
    (`shadow_verdict`/`shadow_similarity`, auto-upgraded columns), so
    `GET /admin/shadow-diff` is a GROUP BY over stored columns: per
    (model, shadow deployment) — pair counts, verdict breakdown,
    match rate, mean similarity over comparable pairs, and latency/
    token/cost deltas against the paired primaries. Scoring happens
    in memory, so verdicts exist even when `logging.store_bodies` is
    off; only the drilldown excerpts need stored bodies.
  - **The drilldown is the review queue** — `GET /admin/shadow-diff/
    pairs?model=…&deployment=…&verdict=divergent` shows both sides
    side by side with paging; texts are null (honestly) on rows whose
    bodies were never stored. The admin dashboard's Routing tab
    renders the same: an evidence table under the routing table and
    a pair-drilldown dialog behind each row.
  - **Honest scoping** — deployment mirrors only: prompt A/B rows
    (also shadow rows) are excluded from the aggregate because their
    divergence is by design, though they carry verdicts on the log
    surface for the prompt workflow; mirrors scored before tier 35
    (verdict NULL) are never re-guessed. Both endpoints are
    platform-admin-only — the pairs carry request bodies, which
    cross tenants (the same boundary as /admin/routing).
  13 tests pin the contract (`tests/test_shadow_diff.py`); the suite
  stands at 1081.
- BYOK hardening (tier 34) — three load-bearing additions to the
  tenant-owned-credential vault:
  - `vault.strict` — the platform-economics stance: tenant traffic may
    ONLY ride credentials the tenant stored. The routing ladder for a
    strict tenant is narrowed BEFORE the walk (deployments whose
    provider the tenant has not keyed are removed — cached vault
    lookups, so the hot path stays a dict read), which means retries
    and fallbacks can never cross to an unkeyed provider; a model
    whose every deployment is unkeyed refuses 403 `byok_key_required`,
    naming the sorted provider list, the model and the exact
    `PUT /admin/tenants/{id}/provider-keys/<provider>` path that fixes
    it. Both ladder heads (buffered and streaming) and the realtime
    connect-time ladder narrow identically — a realtime refusal is
    delivered in-band (OpenAI-shaped error event + close 1008).
    Platform callers (no tenant), platform admins and the platform's
    own machinery (shadow mirrors, canary probes) are untouched;
    undecryptable entries still fail closed (`byok_key_unreadable`)
    through the filter. The stance reads live off the config per
    request — hot-reloadable — while `vault.backend`/`ttl_seconds`
    report `restart_required` (they build the service). Documented
    footgun, made loud: strict with no master key means tenant traffic
    cannot run at all.
  - `vault.backend: redis` — cross-replica cache invalidation: admin
    writes publish `{"tenant_id", "provider"}` on
    `{redis.prefix}:vault-invalidation` and every replica's listener
    task (one per process, in the lifespan) drops the named decrypted-
    key cache entry and its NEGATIVE twin — the cached "no key
    stored" answer that would otherwise keep routing the platform's
    key for up to `ttl_seconds` after another replica stored one.
    Best-effort by design: a failed publish never fails the admin
    write, malformed messages are skipped loudly, Redis outages back
    the listener off 5s, and the TTL remains the backstop. `memory`
    (the default) keeps the single-process shape byte for byte.
  - `GET /admin/vault` (admins) — the honest status surface the
    dashboard needs: `enabled` or the `unavailable_reason` naming the
    env var to set (never a value), the live `strict` stance, the
    `invalidation_backend`, `ttl_seconds`, and `admin_context` — the
    tenant a *tenant admin* manages (the only one the provider-key
    endpoints let them touch); platform admins get null and pick from
    the list. No secrets anywhere in the payload. The admin UI gains a
    Vault tab (between Tenants and Prompts): status panel + the
    credential manager for both admin kinds, key input type=password
    cleared after every store, hints only ever shown.
- MCP proxy — `POST /v1/mcp/{server}` (plus `DELETE` for session
  termination, `GET` answered 405), the sixth inbound surface and the
  first whose payload is not for the gateway's models at all: a
  transparent Model Context Protocol proxy speaking the Streamable
  HTTP transport (spec 2025-06-18) between MCP clients and
  operator-configured MCP servers (`mcp:` in config.yaml — servers
  with url + auth headers, literal or env-resolved, and a session
  TTL), with the gateway in the middle applying its keys, its policy
  and its logs while the servers never know. Wire contract: one
  JSON-RPC message per POST (arrays answer -32600 — the transport
  removed batching; non-JSON -32700; both as transport 400s in the
  JSON-RPC envelope so client SDKs parse the rejection), initialize
  creates a session, notifications and client answers to
  server-initiated requests relay upstream and answer 202, everything
  else requires the Mcp-Session-Id header (400 -32000 missing, 404
  -32003 unknown/expired). The gateway issues its OWN session ids
  (gw_-prefixed) mapped to the upstream session + the negotiated
  protocol version, stamping both on every post-initialize upstream
  call — a client session id is useless upstream, and the registry is
  the gateway's own state (app.state, survives hot-reloads; DELETE
  drops the local mapping first — idempotent for the client — then
  relays the upstream DELETE, 405 allowed; an upstream 404 evicts
  lazily so the next message fails at the gateway and the client
  re-initializes, per the transport spec; a lifespan loop sweeps
  idle sessions after mcp.session_ttl_seconds, closing them upstream
  best-effort). The caller's gateway credential NEVER travels
  upstream — upstream auth is the operator's configured headers, and
  `GET /admin/mcp` (platform admin) introspects each server showing
  the auth-header NAMES only, never the values. Policy spine, per
  name (the tier-30 tool policy): tools/list is filtered to the
  names this key could actually call (a restricted key never learns
  the catalog it is denied), tools/call is admitted one name at a
  time, and a denial is an IN-BAND JSON-RPC error — HTTP 200, -32001
  — so the calling agent sees which tool and why (a transport 4xx
  would hide it from its JSON-RPC layer); nothing upstream ever sees
  a denied call, and a nameless call under a tool policy fails
  closed. Guardrails over the tool traffic: on tools/call the request
  phase runs over the serialized arguments (a synthetic chat request)
  and the response phase over each text item of the result — BLOCK is
  the same in-band error (-32002), MODIFY is applied only while the
  rewritten arguments stay a valid JSON object (else fail closed
  rather than corrupt the call), and a model-redirect verdict fails
  closed (no model exists to redirect on this surface). Server
  allowlists follow the model-permission mechanics: allowed_mcp_
  servers (fnmatch, key layer with platform-admin bypass ∩ tenant
  layer with none — the tenant can only narrow), the 'mcp' endpoint
  scope (chat/embeddings/images/audio/models/realtime vocabulary
  extended; admin-UI key + tenant dialogs gain the field), and
  unknown/not-allowed servers answer 404 exactly like missing ones.
  Both upstream answer shapes work: application/json is intercepted
  (filtering/rewrites applied, re-serialized) and text/event-stream
  is relayed LIVE — progress notifications and keepalives pass
  through verbatim (byte-identical, so client read timeouts never
  fire), and THE response frame per exchange is intercepted,
  re-serialized with policy applied or replaced by an in-band error
  frame; the standalone GET stream (server-to-client outside an open
  POST) is out of scope — GET answers 405, which the spec permits.
  Documented error split: gateway failures (auth, scope, config,
  rate limit, concurrency) keep the OpenAI envelope like every
  surface; protocol-level failures (the codes above, the in-band
  denials) use the JSON-RPC envelope; upstream HTTP failures relay
  verbatim so the client's JSON-RPC layer sees the server's own
  errors. Accounting: one request-log row per message (route mcp,
  provider mcp, model = the SERVER name — the same role a model name
  plays everywhere else, which scopes model-scoped guardrails to
  servers here), gateway_mcp_requests_total{server,method,outcome}
  (success/error/blocked/relayed) + the gateway_mcp_sessions{server}
  gauge, mcp.session.opened / mcp.tool.blocked / mcp.session.closed
  lifecycle events; NO tokens, cost, TPM or budgets — MCP servers are
  not language models; RPM applies (the realtime precedent), token
  economics do not, by design. mcp: hot-reloads (a removed server
  404s the next message; live sessions stay mapped until they expire
  or are deleted). Out of scope, documented: stdio upstreams
  (HTTP-only transport), the standalone GET stream, and BYOK on MCP
  upstreams (no per-tenant notion of "your" weather server). 40 new
  tests (tests/test_mcp.py, behind a scripted JSON-RPC upstream on a
  composite mock transport that routes mcp.test to the MCP mock and
  everything else to the chat mock); full suite 1049 passing.
- Realtime API — `WS /v1/realtime?model=<public-model>`, the fifth
  inbound surface and the first STATEFUL one: a long-lived
  bidirectional event session speaking the OpenAI Realtime protocol
  on both legs, where the gateway holds the matching upstream
  WebSocket open for the session's whole life and applies policy
  where a session actually differs from a request. Open gates run
  ONCE per session, accept-first (every failure to open is delivered
  in-band — an OpenAI-shaped {"type":"error","error":{...}} event
  followed by close 1008, the session-shaped equivalent of the HTTP
  error envelope): the same auth chain and credential namespace as
  HTTP (Authorization/x-api-key/x-goog-api-key/api-key, plus ?key=
  in the query string because browsers cannot set WebSocket headers),
  the 'realtime' endpoint scope (its own — a session is a different
  kind of commitment than a call; chat/embeddings/images/audio/models
  vocabulary extended), model permission, RPM at open (tokens accrue
  per response), the budget check, and a concurrency slot HELD for
  the session's life (max_concurrency counts sessions — they occupy
  upstream capacity like an in-flight stream; the second session
  429s at the open, not at first use). Routing walks the residency-
  and breaker-filtered ladder at CONNECT time only
  (Router.resolve_realtime → per-candidate _resolve_provider_cfg +
  adapter.ws_headers + websockets.connect; a dead leg fails over
  between handshakes, then the session stays put — there is no retry
  protocol for a stream already 40 minutes in); a provider without
  realtime_base is never realtime-routed (realtime_unavailable, never
  a silent HTTP fallback), and the BYOK vault rides the handshake
  like every serving path (a tenant that keyed the provider connects
  with THEIR credential; the row records byok_provider). Per-event
  guardrails: the request phase runs over each response.create —
  over its own input (fork semantics) or the SHADOWED conversation
  the relay keeps from conversation.item.create/delete events when
  the create carries none — and BLOCK kills that response, not the
  session (error event, nothing forwarded upstream); the stream
  phase + held-back rewriting (the same protocol the SSE path uses)
  run over output_text.delta — block checks see the SOURCE text,
  rewriters see what the client receives, the final flush lands at
  output_text.done so nothing a rewriter masks ever leaks no matter
  how the provider chunked; a mid-response stream BLOCK sends the
  error event, forwards response.cancel upstream and suppresses the
  rest of THAT response while its response.done still bills (the
  provider ran) and the session lives on. Budgets re-check on every
  create: exhaustion mid-session is session-fatal (error event +
  close 1008, the row records why). Text is the guarded surface —
  audio deltas and other event types pass through. Accounting: ONE
  request-log row per session (route 'realtime', stream true) with
  tokens/cost summed across responses, usage input→prompt mapping,
  TTFT on the first delta — so budgets, exports, /admin/logs and
  gateway_realtime_sessions{provider,model} see realtime traffic
  exactly like HTTP. Deliberately not on this surface: the response
  caches, idempotency, runaway detection and X-Gateway-* headers (a
  WebSocket has no response headers). Requires websockets>=12
  (pyproject base dependency). 17 new tests (tests/test_realtime.py —
  hand-rolled ASGI WSClient + a websockets-server mock upstream that
  scripts responses per create with inter-delta yields so mid-response
  blocks cancel mid-burst); full suite 1009 passing.
- BYOK vault — tenants bring their OWN provider credentials, encrypted
  at rest: one Fernet-encrypted entry per (tenant, provider)
  (`PUT /admin/tenants/{id}/provider-keys/{provider}`,
  `GET .../provider-keys`, `DELETE .../provider-keys/{provider}`), and
  every request from that tenant through that provider rides THEIR
  account instead of the platform's configured key — chat (stream and
  not), embeddings, images, audio, through every retry and fallback
  leg (per-candidate resolution inside the router's dispatch loop, so
  the stored key never leaks onto a fallback provider the tenant
  didn't key; that leg alone rides the platform's). The swap point is
  Router._resolve_provider_cfg: a copy of the provider config with
  api_key set / api_key_env cleared — every adapter already reads
  credentials through provider_cfg.resolve_key(), so the tier needed
  ZERO adapter changes. The raw key exists exactly twice: the caller's
  PUT body and the upstream auth header. At rest: Fernet (AES128-CBC +
  HMAC-SHA256) under GATEWAY_VAULT_MASTER_KEY (env only, never
  config.yaml — it encrypts secrets; comma-separated list = MultiFernet
  rotation: first encrypts, all decrypt). Never surfaced: the admin API
  returns only a hint ("…1234" — last chars, enough to tell stored keys
  apart, never enough to use), audit records carry the same hint
  (vault.provider_key.upsert/delete events), log rows record only
  byok_provider (the provider name) alongside the
  X-Gateway-Byok-Provider response header and the
  gateway_byok_requests_total{provider} counter — the
  chargeback-compatibility metric. Fail closed where it matters: an
  entry that cannot be decrypted (a rotated-away master key) fails the
  request with 500 byok_key_unreadable instead of silently spending
  the platform's key; fail honest when off (no master key): PUT 400s
  vault_unavailable naming GATEWAY_VAULT_MASTER_KEY, reads stay empty,
  every caller byte-for-byte unchanged. Hot path: decrypted entries
  cached per (tenant, provider) for vault.ttl_seconds (30 default,
  invalidate-on-write; other replicas converge within the TTL —
  documented; Redis-shared invalidation deferred). Tenant admins
  manage their own tenant's credentials only (cross-tenant 404, the
  same anti-leak semantics as key management); platform machinery
  (shadow mirrors, canary probes, the semantic-cache embedder) rides
  platform credentials by design. Requires pip install
  'llm-gateway[vault]' (cryptography). 11 new tests
  (tests/test_byok_vault.py); full suite 992 passing.
- Tool-call policy (MCP-lite) — the agent-era sibling of model
  allowlists: `allowed_tools` on a virtual key (and pooled on its
  tenant) is a list of fnmatch patterns over the tool NAMES a request
  may declare, with exactly allowed_models' mechanics (None = all
  tools, an explicit-null PATCH clears, admin API + admin UI fields
  on the key/tenant dialogs with b-teal badges on the rows). One
  shared check runs at phase 1.1, right after model permission, and
  every inbound surface funnels through it because all of them —
  OpenAI chat, the Responses bridge, native Anthropic, native Gemini
  — normalize tools into the OpenAI shape before the pipeline.
  Two layers like every ceiling: the key's patterns (platform admins
  bypass — they are the operators) and the tenant's pooled patterns
  (no bypass; binds every member, tenant admins included — a tool
  must pass BOTH lists, so the tenant can only narrow). A request
  declaring a tool that matches neither list fails with a 400
  tool_not_allowed naming up to five offenders (the caller sent the
  names — nothing to leak, everything to debug). Guardrails can
  overlay tools (request-phase modified_request_fields); the policy
  is re-checked at 4.6 after the guardrail phase, so a guardrail can
  never inject a tool past the key's allowlist — the same hard
  boundary model redirects get at 4.5. A tool whose name cannot be
  extracted (an exotic future tool type) fails closed as <unnamed>.
  Requests without tools pass trivially, and callers with no
  allowed_tools anywhere are byte-for-byte unchanged. 7 new tests
  (tests/test_tool_policy.py); full suite 981 passing.
- Prompt compression guardrail — the remediation counterpart of
  context_window: where that guardrail blocks (or redirects) an
  over-window request, prompt_compression deterministically
  COMPRESSES it to fit — middle-out folding, no LLM call, no added
  latency. Always keeps leading system messages plus the first
  keep_first and last keep_last non-system messages, drops the middle
  with a marker note prepended as CONTENT to the first kept message
  (roles and content-part shapes preserved — a parts-list message
  gains one leading text part, images ride along untouched — so every
  provider adapter and native outbound format serves the folded
  request exactly like the original). If still over the target,
  truncates kept messages oldest-first toward min_message_chars;
  never trims the system block or the last message (a response to a
  mangled task or latest turn is garbage either way). Never blocks:
  when it cannot reach the target (a gigantic last message) it steps
  aside and the request flows on untouched — context_window, ordered
  after it, gives the honest error. Target: max_input_tokens when set
  (an explicit budget — cost control even on models whose window
  would never block), else the declared window minus
  reserve_output_tokens (same vocabulary and default as
  context_window, so compress-then-check composes: a request folded
  under the target passes the check that follows). Framework
  composition for free: observe mode logs would-be savings without
  touching the request, scoping limits it to keys/models/routes,
  per-key disabled_guardrails turns it off at runtime, verdicts land
  in guardrail_actions with tokens_before/after/saved and
  messages_dropped/truncated, and enforced savings aggregate on
  gateway_prompt_compression_saved_tokens_total{model} (the ROI
  metric; observe mode and replay-harness runs never count — the
  GUARDRAIL_BLOCKS discipline applied to savings). Modifications run
  before the cache lookup, so compressed and uncompressed forms of
  one logical prompt hold different cache entries. 18 new tests
  (tests/test_prompt_compression.py); full suite 974 passing.
- Data-residency routing — deployment-tag-constrained routing, fail
  closed. Each deployment may carry an operator-asserted `residency`
  tag (lowercase slug, e.g. "eu" — config.yaml; None = NO CLAIM,
  which satisfies no requirement: no claim, no compliance), and
  virtual keys / tenants carry `required_residencies` (admin API +
  UI: comma-separated tag fields on the key/tenant dialogs, badges
  on the rows). The caller's EFFECTIVE set is the intersection
  key ∩ tenant (the tenant can only narrow; an empty intersection
  means nothing can serve). The filter lives inside Router.resolve
  — BEFORE the priority ladder, retries and fallbacks — so a
  constrained request can never escape to a non-compliant
  deployment mid-failover; failures still fail over WITHIN the
  compliant class (residency narrows the ladder, never disables
  it); no compliant deployment answers 404 residency_not_satisfied
  naming the model and the required tags, and nothing is ever
  consulted upstream. Threaded through every modality: chat (all
  inbound formats, stream and not), embeddings, images, audio —
  and SHADOW MIRRORS too (a mirror is a copy of the caller's data:
  non-compliant shadows silently receive nothing). Caches respect
  the boundary without losing reuse: the requirement salts the chat
  response-policy token and the embeddings cache key ONLY when one
  exists (residency classes never share entries; unrestricted
  callers keep their exact pre-tier-28 keys, existing entries stay
  valid) — this fixed a real leak found by the new tests, where the
  tenant-salt-only embeddings cache key could serve a restricted
  caller an entry generated on a non-compliant deployment. The
  semantic cache degrades, never violates: a restricted caller's
  prompt is only embedded through the router's residency filter
  (the residencies argument is passed to the embedder only when a
  requirement exists, so external single-argument embed fns keep
  working for unrestricted callers, and a restricted caller whose
  embedder cannot accept it is a plain miss). Semantics: exact
  matching after normalization (^[a-z0-9][a-z0-9_-]*$; EU and eu are
  the same tag, anything else is a 400 at the API and a load error
  in config), PATCH [] and null both clear, admin/env-key traffic
  exempt, GET /admin/routing shows each deployment's residency
  claim. 14 new tests (tests/test_residency.py); full suite 956
  passing. Docs: README features/routing/admin-API rows + a
  dedicated section; ARCHITECTURE router module-map extension, ER
  diagram (also backfilled pricing_markup_pct / max_concurrency /
  priority — a tier-25/27 doc gap), replay-harness note (the
  ladder prediction resolves with the CURRENT key's residency) and
  lifecycle-diagram routing step.
- Concurrency admission control + QoS — per-key and pooled
  per-tenant in-flight caps with priority queueing. RPM/TPM bound
  the rate of requests; max_concurrency (VirtualKey, and the pooled
  Tenant.max_concurrency a member key must also pass — the tenant
  can only narrow) bounds how many may be in-flight against the
  upstream at once, the number that actually protects a provider
  quota or a small deployment fleet. The slot wraps exactly the
  upstream call: acquired at phase 5.5 after the cache miss (cache
  hits and idempotent replays never hold one), released wherever
  the in-flight gauge decrements — _finish_span for non-streams,
  the stream wrapper's finally for streams (a slow stream reader
  occupies upstream capacity for exactly as long as it reads), and
  every error path, so a leaked slot is structurally impossible.
  At the cap requests QUEUE (no extra upstream pressure): waiters
  sit in a heap ordered (-priority, seq) — VirtualKey.priority is
  the QoS class, higher admitted first, FIFO within a class, only
  meaningful under contention (a key attribute, so it matters
  exactly where contention crosses keys, typically the tenant pool)
  — and the drain on every release scans in priority order,
  SKIPPING rather than stopping at waiters that do not fit, so a
  waiter on one saturated key never blocks an unrelated one.
  asyncio.wait (never wait_for — a granted slot is not cancelled)
  bounds the stay at concurrency.wait_seconds; expiry answers 429
  rate_limit_exceeded + Retry-After (0 = fail fast), fires a
  concurrency.timeout observer event (key, model, cap, wait and
  waited seconds) and counts gateway_concurrency_rejected_total
  {key}. Gauges gateway_concurrency_inflight{key} /
  gateway_concurrency_queued{key} are the limiter's ground truth
  (capless callers skip admission entirely and never appear);
  GET /admin/concurrency surfaces them with key/tenant name joins
  and the live config echo (platform admin only, pinned in the
  platform-endpoint enumeration). Admin UI: Max concurrent +
  Priority fields on the key dialog, Max concurrent on the tenant
  dialog, with badges on the key/tenant rows. Honest boundaries,
  same as runaway: chat-family only (embeddings/images/audio run
  through _serve_simple and never acquire a slot); admin/env-key
  traffic exempt; no locks anywhere (event-loop atomicity);
  in-process state (like the idempotency in-flight map and runaway
  detection) — multi-replica fleets enforce per-replica shares,
  run N replicas with cap/N or accept the approximation.
  priority is nullable-default-0 so db.py's auto-upgrade can add
  it to existing databases. 15 new tests (tests/test_concurrency.
  py); full suite 942 passing. README's event catalog also gained
  the runaway.trip / runaway.released rows (a tier-26 doc gap
  noticed while adding concurrency.timeout).
- Runaway-agent protection — identical-request loop detection with an
  escalating cooldown jail. Agents stuck in loops (retry storms,
  regenerating tool-call loops) send the SAME request over and over;
  every one is individually legal, so RPM/TPM ceilings cannot see
  them. The detector fingerprints each chat-family request with the
  same normalized identity the response cache and idempotency key on
  (sha256 of cache_payload_for — stable across OpenAI chat /
  /v1/responses / native Anthropic & Gemini, and stream is not part
  of the identity, so a loop alternating stream flags is one loop)
  and counts hits per (key, fingerprint) inside a sliding window;
  reaching the threshold (default 10 in 60s) jails the KEY: every
  request from it answers 429 rate_limit_exceeded + Retry-After
  until the cooldown expires — including would-be cache hits and
  idempotent replays, because nothing a stuck agent sends is useful.
  The tripping request itself is refused. Re-trips escalate
  geometrically (30s -> 60s -> ... capped at 1h; the offense ledger
  survives jail expiry, decays after a quiet reset_after_seconds),
  and loop windows are CLEARED on trip: after the cooldown a human's
  single investigation retry passes while a still-looping agent
  re-trips on fresh hits. Phase 1.5 in _handle — before rate limit,
  idempotency, budget and cache. Admin/env-key traffic (no key_id)
  is exempt, like every key-layer ceiling; embeddings/images/audio
  (_serve_simple) are out of scope, honestly: single-shot surfaces,
  not loops. Thresholds read the live config per call (hot reload
  applies; jail state survives, same trade the router makes with
  breakers); detection state is in-process (like the idempotency
  in-flight map): a multi-replica fleet detects at per-replica rate
  — bounded, just slower to trip. The clock is injectable for tests.
  Surface: runaway: config section (enabled, threshold, window,
  cooldown, multiplier, cap, reset_after); observer-only
  runaway.trip event (key_name, model, count, threshold, window,
  cooldown_seconds, offenses — never interceptable, the jail is the
  decision); gateway_runaway_trips_total{key} counter (KeyLabeler-
  capped cardinality, always emitted); GET /admin/runaway (platform
  admin: jail snapshot with remaining cooldown/offenses + key-name
  join + live config echo) and POST /admin/runaway/{key_id}/release
  (escape hatch — clears jail, offense history and loop windows, so
  a re-trip starts at the first-offense cooldown; audited as
  runaway.released). 14 new tests (tests/test_runaway.py).
- Tenant pricing markup — cost-plus billing for resellers/chargeback.
  A tenant can carry pricing_markup_pct (e.g. 20.0 = bill member-key
  traffic at 1.2x provider cost), applied to every modality and every
  member key, tenant admins included. The BILLED cost —
  provider_cost * (1 + pct/100), rounded to the calculator's 8
  decimals — is what spend_usd (key + tenant), the budget windows,
  budget alerts, RequestLog.cost_usd (/admin/logs, the capture
  export, /admin/usage/export for chargeback, /admin/stats by-tenant
  aggregates), the per-key gateway_key_cost_usd_total metric and the
  X-Gateway-Cost-Usd response header record;
  gateway_cost_usd_total{model} deliberately keeps the ACTUAL
  provider cost so platform economics stay visible beside tenant
  economics, and a new gateway_markup_usd_total{tenant} counter
  accumulates the markup revenue itself (billed minus actual).
  Implementation: a shared pipeline._billed_cost helper called from
  both accounting tails — _account_usage (chat/embeddings/streams)
  and _account_cost (unit-priced images/audio/speech) — so every
  modality marks up consistently. Semantics: the markup snapshots at
  auth time with the rest of the tenant ceiling (a PATCH applies from
  the next request, never mid-flight); budgets enforce the BILLED
  cost (a tenant's ceiling is what they pay, not what the platform
  pays); platform keys, NULL and explicit 0 all bill at actual;
  shadow-mirror rows keep actual provider cost (mirror rows compare
  deployments, they don't bill anyone). Surface: Tenant row field
  (nullable Float, auto-upgrade adds it; exposed in to_public_dict),
  POST/PATCH /admin/tenants (validated 0..1000 percent; body
  validation surfaces as the usual 400), and the admin UI tenant
  form + list column. 10 new tests (tests/test_tenant_markup.py).
- Cost-aware routing + cached-token billing — two halves of one
  concern: what traffic costs.
  - Cached-token billing: providers discount prompt-cache reads
    (OpenAI prompt_tokens_details.cached_tokens, Anthropic
    cache_read_input_tokens at 10% of base input, Gemini
    cachedContentTokenCount) and the gateway now bills them. Adapters
    normalize every provider's report into the OpenAI wire shape with
    one internal contract — cached tokens are a subset of
    prompt_tokens (Anthropic's out-of-band cache reads and cache
    writes fold into the prompt count; Gemini's promptTokenCount
    already includes them) — and the calculator bills
    (prompt−cached)×input + cached×cached_input + completion×output.
    cached_input is a new per-model pricing key defaulting to 50% of
    input when omitted (OpenAI's discount; the built-in table encodes
    Anthropic's 10% and Gemini's ~25%; operators can set it
    explicitly). The split surfaces everywhere: the OpenAI response's
    usage details (passthrough and translated), native Anthropic
    cache_read_input_tokens, native Gemini cachedContentTokenCount,
    the request-log row (new cached_tokens column — auto-upgrade
    adds it; /admin/logs, the capture export and the usage export for
    chargeback all carry it), and gateway_tokens_total
    {type=cached_input} / the per-key family when metrics.per_key is
    on. cached_tokens_from_usage is tolerant of flat-field
    OpenAI-compatible servers and clamps to prompt_tokens so a lying
    upstream cannot bill negative input.
  - Cost-aware routing: routing.cost_aware (default false) ranks
    same-priority deployments by their upstream model's INPUT price —
    the pricing table with the same longest-prefix lookup billing
    uses, via a CostCalculator the router rebuilds on config swap so
    pricing hot-reloads apply. Cheapest first; the price ladder
    doubles as the failover ladder (the dispatch loop re-picks from
    the remainder); weight only breaks price ties (equal prices
    load-balance by weight as before); unpriced models sort last
    (unknown cost gets no discount); priority still rules across
    tiers (cost never outranks a deliberately lower-priority
    deployment). /admin/routing echoes the cost_aware flag and each
    deployment's input/output/cached_input_usd_per_mtok so the
    ranking is inspectable.
  - 15 new tests (tests/test_cost_aware.py): extraction shapes and
    clamping, billing math (default + explicit cached_input, prefix
    match, unpriced), adapter folds (Anthropic non-stream + stream,
    Gemini non-stream + stream), e2e billing/logging/metrics on the
    OpenAI surface, native Anthropic/Gemini round trips, and the
    routing ladder (cheapest-first, failover walk, unpriced-last,
    equal-price weighting, cost_aware-off baseline,
    priority-outranks-cost, /admin/routing echo). test_usage_export
    and test_images pin updated contracts (cached_tokens column;
    3-tuple lookup).
- Capture-replay harness: a config-change safety net built on the
  request log. GET /admin/logs/export captures the replayable traffic
  as JSONL (rows with stored bodies; shadow mirrors excluded; same
  scoping/filters as /admin/logs) with each line's recorded baseline —
  caller identity, inbound route, model, status, deployment, guardrail
  verdicts, plus comparable/skip_reason. POST /admin/replay re-executes
  captured requests through the request-policy phases ONLY — model
  resolution, prompt policy, permissions, request-phase guardrails,
  redirect re-validation, routing selection (the priority ladder, no
  dispatch) — then diffs each line against the record: changed lines
  are what the current config would change about that traffic. Replays
  are dry runs: no upstream calls, no rate-limit/budget/idempotency
  counters, no cache, no spend, no request-log rows, no lifecycle
  events — and ctx.dry_run suppresses the guardrail enforcement/
  observed counters in the manager, so a harness run can never trip
  block alerts. Line outcomes count on
  gateway_replay_total{result=changed|unchanged|not_comparable|error}.
  The caller is replayed at their CURRENT policy — the key row is
  re-loaded fresh, so keys edited or revoked since the capture surface
  as findings (would now 401) rather than being assumed to still work;
  entries with no virtual-key identity (bare requests, admin-key
  captures) replay as the calling admin's context; tenant admins skip
  other tenants' traffic ("entry belongs to another tenant").
  Comparability is honest: rows recorded as error predict but don't
  diff (upstream/counter outcomes are state, not policy), response-/
  stream-phase verdicts are not compared (replay stops at the request
  phase), auth-rejection predictions flag status only (model is None),
  and prompt-policy application is reported
  (prompt_policy_applied) rather than diffed. To make replay run the
  exact policy code live traffic does, steps 0-1 and 4-4.5 were
  extracted from GatewayPipeline._handle into shared helpers
  (_prepare_request_policy / _apply_request_guardrails) used by both
  paths (extraction is behavior-neutral — the full suite pins it).
  RequestLog gains key_id (the exact calling key) and route (the
  inbound surface) columns — nullable, auto-upgrade adds them —
  persisted by the logging service; /admin/logs gains a ?key_id=
  filter. config.yaml: a replay: section (enabled — POST /admin/replay
  answers 404 replay_disabled when false — and max_entries, default
  500, a 400 beyond; both read live off the running config, so hot
  reload applies). 16 new tests (tests/test_replay.py).
  Implementation note: export filters NULL bodies in Python, not SQL —
  SQLAlchemy's JSON type stores Python None as the literal string
  'null' on SQLite, so an IS NOT NULL filter would diverge between
  the SQLite and PostgreSQL/JSONB variants.
- Context-window management: declare `context_window` (total tokens,
  input + output) once per public model and the `context_window`
  guardrail fits incoming requests to it *before* they burn upstream
  quota — estimated input (~4 chars/token, the same heuristic as TPM,
  including the managed system prompt) plus the response budget (the
  caller's max_tokens, else reserve_output_tokens) must fit. Over-window
  requests get an OpenAI-style context_length_exceeded 400 with the
  actual numbers ("maximum context length is 1000 tokens... 5001
  total"), or — with on_exceed: redirect — are served by a bigger model
  instead (reject-and-redirect, allowlist re-checked fail-closed). The
  window is model metadata (models[].context_window), resolved onto the
  request context at model resolution and re-resolved when another
  guardrail redirects; the guardrail's params are policy: window
  (force a number), default_window (govern models that declare none —
  no declaration and no default = unmanaged, passes through) and
  reserve_output_tokens (1024 default). Strictly-greater blocks:
  input + max_tokens == window fits. Blocked requests 400 pre-dispatch
  on every chat-family surface (streams included — the envelope, not
  an SSE body); embeddings/images/audio unaffected. Like any guardrail
  it composes with observe mode, scoping and per-key
  disabled_guardrails. /admin/routing now echoes each model's
  context_window. config.yaml: gpt-4o declares 128000 + a documented
  disabled context_window guardrail entry. 14 new tests
  (tests/test_context_window.py).
- Idempotency keys: retry-safe chat completions on every chat-family
  surface (OpenAI chat, /v1/responses, native Anthropic /v1/messages,
  native Gemini :generateContent). A client sends an Idempotency-Key
  header; the *successful* response is stored under (caller, client
  key) and replays on retry — no double generation, no double billing
  (the stored usage was billed at first execution; replays are
  zero-cost cache_hit log rows answering X-Gateway-Cache: IDEMPOTENT,
  streams re-synthesized from the stored completion). Only successes
  are stored: errors and guardrail blocks re-execute, which is what a
  transient failure needs. Reusing a key with different request
  parameters is a 400 (idempotency_key_reuse) — the fingerprint is
  the original request as received, the same normalized identity the
  response cache keys on, stashed on the request context before
  guardrails can rebind the request. In-flight duplicates (the
  canonical case: retrying a timed-out request while the original
  still runs) wait bounded by inflight_wait_seconds for the original
  — shielded, so a waiter's timeout never kills it — then replay it,
  or execute themselves if it failed or wedged (last writer wins);
  the map is in-process, so the Redis backend dedups only completed
  entries cross-replica (documented). Phase 2b in the pipeline:
  rate-limit → idempotency → budget, deliberately — a retry must
  still serve even if the caller's ceiling was crossed after the
  original was authorized; new requests still hit the ceiling.
  Entries are private per caller (store key "idem:<key id>:<client
  key>"); keys are 1-255 non-whitespace chars (400
  invalid_idempotency_key). Embeddings/images/audio and batch lines
  ignore the header. No header = zero behavior change;
  idempotency.enabled=false ignores the header entirely. Backend
  selection mirrors the response cache (memory LRU / Redis — its
  keys namespaced at <prefix>:idem:cache:* with their own TTL/LRU);
  config: idempotency section (ttl_seconds=86400 — Stripe's window —
  max_entries=10000, inflight_wait_seconds=30, backend). Outcomes
  count on gateway_idempotency_total{outcome=replayed|stored|
  rejected}; replays emit request.cache_hit with kind=idempotency.
  Idempotency joins restart_required on reload (backend is shared
  state, like the cache). conftest.make_config grows idempotency=
  and a proper cache= override. 14 new tests
  (tests/test_idempotency.py).
- PII/secret scan over stored bodies: an on-demand audit that
  answers "did secrets or PII land in the request log?" Clients
  paste API keys into prompts; guardrails may be off, in observe
  mode, or only masking the upstream copy — the log keeps the
  original as received (ctx.request_body is captured before the
  guardrail phase), so stored rows are where exposure actually
  shows. Redaction protects the provider; this reports what the
  retention policy is holding. Endpoints (pure reads, nothing
  persisted): GET /admin/pii-scan — bulk over recent rows (limit /
  key_name / model / status / tenant_id filters, bounded by
  pii_scan.max_rows), returning per-row findings for flagged rows
  plus by_kind aggregates — and GET /admin/logs/{id}/pii-scan for
  one row. Detectors mirror the pii_redaction guardrail (the same
  compiled patterns, imported from it — "masked" and "found" can
  never drift) plus jwt / private-key-block secret shapes and
  Luhn-validated cards; custom detectors via
  pii_scan.extra_patterns (name -> regex, validated at load),
  built-ins droppable via exclude_kinds. Findings are kind + count
  only — never the matched text, so the audit never becomes its own
  leak. clean and bodies-not-captured are separate facts
  (store_bodies=false rows report captured=false, never "safe").
  Tenant scoping identical to /admin/logs (bulk forced for tenant
  admins; foreign single rows 404). Flagged rows count on
  gateway_pii_scan_findings_total{kind} (rows containing the kind,
  not raw occurrences) and emit pii.found events. Scanner rebuilt in
  create_app and on config swap. Config: pii_scan section (enabled,
  max_rows, extra_patterns, exclude_kinds). Caveats: stored bodies
  are truncated at logging.max_body_chars — a secret past the
  boundary is invisible to the scan. 10 new tests
  (tests/test_pii_scan.py).
- Synthetic canary probes: a background loop sends a tiny chat request
  straight to every configured deployment's provider on a schedule
  (`canary:` config — enabled / interval_seconds / timeout_seconds /
  max_tokens / prompt / deployments fnmatch allowlist; disabled by
  default, probes spend real provider quota), so upstream health is
  measured, not inferred from traffic — a dead fallback that never
  receives traffic otherwise reads as "healthy" until the moment it's
  needed; shadow deployments only ever see mirrored traffic. Probes
  are direct adapter calls: no pipeline, no guardrails, no cache, no
  request-log rows, no spend, no circuit-breaker accounting — they can
  neither affect nor be mistaken for client traffic, and never
  half-open a breaker. Every configured deployment is probed
  (primary/fallback/shadow alike); an optional fnmatch allowlist
  trims the fleet. Outcomes ok / timeout (provider never answered
  within the probe's own clock) / error. Failed probes emit
  canary.failed events; a deployment returning to ok after a failure
  emits canary.recovered (first waves have no transitions). Admin:
  GET /admin/canaries (fleet view, config echo; platform-admin-only
  like /admin/routing) and POST /admin/canaries/probe (wave on
  demand). Counted on
  gateway_canary_probes_total{deployment,provider,outcome}. The
  lifespan loop always runs — idling at a 30s re-check when disabled
  — so hot-enabling takes effect immediately; enabled / interval /
  timeout / allowlist re-read each iteration. 8 new tests
  (tests/test_canary.py).
- Feedback capture: `POST /v1/feedback` — clients rate any logged
  request by its `X-Request-Id` (rating 1..5, optional comment),
  turning request logs into quality data. The missing half of the
  shadow/A/B story: mirroring compares outputs, feedback says which
  one was good. Ownership follows the files/batches rule — the
  request-log row must belong to the calling key (platform admin
  exempt), foreign and unknown ids get the same 404 (never confirm
  another key's request exists); rides the `chat` endpoint scope;
  multiple signals per request allowed (corrections). Any logged
  request is rateable — successes, streams and failures alike.
  key/model/tenant denormalized from the log row at feedback time
  (stats never join, and stay stable if a model is later deleted).
  Admin: `GET /admin/feedback` (key/model/request_id/rating filters)
  and `GET /admin/feedback/stats` (overall / by-model / by-key
  buckets: count, avg, pct_negative [1-2], pct_positive [4-5]) —
  the same forced tenant scoping as /admin/logs. Counted on
  `gateway_feedback_total{rating}`; `feedback.recorded` lifecycle
  event. New `feedback` table (existing databases auto-upgrade via
  create_all). 11 new tests.
- Lifecycle events: a publish/subscribe layer over the request stages
  (`core/events.py`). The pipeline and the surfaces around it (auth
  edge, audit choke point, budget alerts, batch API/worker) emit
  named events; configured handlers run at them — with no handlers,
  every emission is a no-op lookup (the bus is inert by design, and
  the full suite runs in that mode). Two handler kinds, registered in
  `events.handlers` config (same `pkg.module:ClassName` convention as
  custom guardrails, params from YAML, hot-reloadable, build-first so
  a bad registration is refused with the old set live): **observers**
  (default) fire-and-forget on a bounded background queue — a slow or
  crashing handler can never add latency to (or fail) a request,
  crashes are logged and counted, a full queue drops (counted) rather
  than blocking, shutdown drains — and **interceptors**, awaited
  in-band at the two pre-irreversible events only
  (`request.received`, `response.completed`, explicit names,
  validated at registration), which may raise `GatewayError` to abort
  the request through the normal error path (log row, metrics,
  client envelope; any other exception is swallowed, logged and
  counted — a buggy handler never 500s traffic). Interceptors inspect
  and veto but never modify content — mutation stays guardrails' job.
  Catalog: `request.received` / `routed` / `cache_hit` / `rejected` /
  `failed`, `response.completed` (with `surface` for
  embedding/images/transcription), `stream.started` / `completed`
  (coarse only — never per-chunk), `auth.rejected`, admin actions
  under their audit names (`key.*`, `tenant.*`, `breaker.reset` —
  same payload as the audit webhook), `budget.threshold` (per crossed
  level), and `batch.created` / `cancelled` / `completed` / `failed` /
  `deferred` / `expired`. Handler classes subclass
  `gateway.core.events.EventHandler` and receive a `GatewayEvent`
  (payload = emission-time snapshot; `ctx` = the live RequestContext,
  final state by delivery time). Pattern subscriptions (`request.*`)
  supported; `emit()` enqueues observers before an interceptor's abort
  propagates so subscribers see the vetoed event. Deliveries count on
  `gateway_events_total{handler,event,outcome}` (delivered / dropped /
  error / aborted). Config: `events:` section (`enabled`,
  `queue_size`, `handlers[]` with `class` / `kind` / `events` /
  `params`) — hot-reloadable (queue_size applies at restart).
  20 new tests.
- OpenAI-style Batch API (`/v1/files` + `/v1/batches`) for bulk chat
  workloads: upload a JSONL of chat requests
  (`POST /v1/files`, multipart `file` + `purpose: "batch"`, capped at
  `batch.max_file_bytes` — 20 MB), create a batch
  (`POST /v1/batches`) and poll `GET /v1/batches/{id}`; when completed,
  `output_file_id` (succeeded lines) and `error_file_id` (failed lines)
  hold OpenAI-shaped JSONL — one `{"id", "custom_id", "response":
  {status_code, request_id, body}, "error": null}` line per input.
  Input is validated synchronously at create (JSONL line rules,
  bodies parsed as chat-completion requests, `max_requests` cap — no
  "validating" status), so a created batch is always runnable.
  Execution is a background worker: `wave_size` lines of the oldest
  runnable batch per `poll_interval_seconds` tick, each line a full
  interactive request through `handle_chat` — guardrails (a blocked
  line fails with the same 400 the client would get, into the error
  file), model allowlists, routing/retries, response cache, spend
  accounting and a request-log row (auth_method `batch`), so batch
  traffic lands in `/admin/logs`, `/admin/stats` and usage exports
  like any other traffic. Rate limits and budget ceilings never fail
  a line — the batch defers (`retry_not_before`, budgets checked per
  wave against a fresh key snapshot) and the 24h completion window
  bounds deferrals into expiry. Crash-safe by cursor: results append
  one row per line in the same transaction that advances `next_line`;
  a restart resumes from the cursor ("finalizing" is the checkpoint
  between all-results-in and files-written). Cancellation is a
  guarded update that never overwrites a terminal state. Files and
  batches belong to the creating virtual key (non-owner ids 404);
  the platform admin key cannot own them (no key row to bill), and
  scoped keys need the new `batch` endpoint scope. `batch:` config
  (enabled/max_file_bytes/max_requests/wave_size/poll_interval_seconds,
  hot-reloadable — the worker re-reads it each tick); progress on
  `gateway_batch_lines_total{status=completed|failed|deferred}`;
  existing databases auto-upgrade (three new tables via create_all).
  16 new tests.
- TTFT + decode rate on streamed request-log rows: streaming latency
  was a single `latency_ms` number; rows now also record `ttft_ms` —
  milliseconds from request start to the first *content-bearing* chunk
  off the wire (role chunks arrive back-to-back with content, so this
  is when the client perceives generation starting; the first chunk at
  all serves as fallback when none ever carries content) — and
  `/admin/logs` rows expose the derived `decode_tokens_per_sec`
  (completion tokens per second AFTER the first token: the generation
  phase, with the model-start wait excluded). Provider-side by design:
  cache-hit replays record NULL (nothing went on the wire — the same
  rule as wire capture), non-streaming rows record NULL, and the
  existing Prometheus `gateway_stream_ttft_seconds` histogram remains
  the client-side counterpart (first chunk delivered, cache hits
  included). `/admin/stats` reports `ttft_ms` p50/p95/p99 over streamed
  rows; the per-request console record carries `ttft_ms` for streamed
  requests; the dashboard shows a TTFT p95 overview card, a ttft/tok-s
  line under each streamed row's latency, and ttft + decode rate in
  the log detail. Existing databases auto-upgrade (new nullable
  column).
- HTTPS serving via `python -m gateway` (wrapped by `scripts/gateway.sh`):
  three certificate modes behind flags — `--self-signed [DIR]` (development
  pair generated on first use under `deploy/certs/` and reused after;
  SANs cover `localhost`/`127.0.0.1`/`::1`, key written 0600),
  `--ssl-cert`/`--ssl-key` for any PEM pair (private/organizational CA —
  also `GATEWAY_SSL_CERT`/`GATEWAY_SSL_KEY` env vars, flags win), and
  `--letsencrypt DOMAIN` resolving certbot's standard
  `/etc/letsencrypt/live/DOMAIN/` layout with a certbot hint when absent.
  Plain HTTP when no certificate is configured (unchanged behavior);
  `--host`/`--port`/`GATEWAY_HOST`/`GATEWAY_PORT` and `--reload` for
  development. Certificates load once at bind time, so restart after
  certbot renews (startup logs the expiry date as a reminder); TLS
  misconfiguration surfaces as a CLI usage error, not a stack trace.
  With HTTPS on, `--redirect-http [PORT]` (default 80) adds a plain-HTTP
  listener that answers every request with a 308 Permanent Redirect to
  the same URL on HTTPS — method-preserving (POST bodies survive),
  Host-header aware (works for every served domain), IPv6-safe, and
  refusing to start without a certificate mode (a redirect to plain
  HTTP would loop).
  Verified end-to-end by a test that serves the real app over TLS and
  calls it with a certificate-verifying client.
- Wire capture on request-log rows: beyond the stored request/response
  bodies, each row now records the allowlisted client request headers
  (which SDK made the call — `user-agent`, the OpenAI SDKs'
  `x-stainless-*` telemetry — and client correlation ids; never
  credentials), the request **as actually sent to the provider** —
  method, path and provider-native body, i.e. after model mapping,
  prompt injection, guardrail edits and provider adaptation — and the
  allowlisted provider response headers (`x-request-id` / `request-id`
  for support-ticket correlation, `retry-after`, `x-ratelimit-*` /
  `ratelimit-*` state). Allowlists are strict (allow, never block, so a
  new credential-bearing header can never leak into stored rows by
  default; `Authorization`, `x-api-key`, `x-goog-api-key` and `api-key`
  are deliberately absent) and configurable via
  `logging.client_header_allowlist` / `logging.upstream_header_allowlist`
  (lowercase names; trailing `*` matches prefixes, e.g. `x-ratelimit-*`).
  The upstream URL is stored as a path only — Gemini's `?key=` lives in
  the query string. `store_bodies: false` suppresses the upstream body
  (traffic content) while keeping method/path and the header snapshots
  (metadata, like tokens/latency). Cache hits record no upstream fields
  (nothing went on the wire); shadow/prompt-A-B mirror rows capture
  their own calls' wire data. Existing databases auto-upgrade: the three
  new nullable columns are added on startup, old rows simply have them
  NULL. Visible in the dashboard's log detail (client headers next to
  the request body; upstream request + upstream response headers next to
  the response body).

### Fixed

- `tests/test_batch.py::test_batch_defers_on_budget_and_resumes` was
  load-flaky: line 0's spend is a fire-and-forget task, and the test
  relied on it landing between two back-to-back worker ticks — under
  a loaded event loop it could land after the wave, letting line 1
  complete instead of deferring. The test now awaits
  `pipeline.drain_background_tasks()` between the waves, making the
  deferral deterministic (test-only change).
- Admin dashboard (Logs tab): an expanded request's detail (request/response
  bodies, guardrail actions) collapsed back to a "loading…" placeholder and
  never recovered — the 5-second auto-refresh re-renders the table, which
  recreates the detail cell, but only the row-click handler ever filled it.
  The fill now lives in `fillLogDetail`, called from `loadLogs` after every
  render (the same pattern the Prompts tab already used), so an expanded
  row survives auto-refreshes, filter changes and pagination.
- Default text-mode console logging showed no per-request detail: the
  pipeline logs one `chat request completed` INFO record per request
  (model, provider, deployment, tokens, cost, latency), but plain uvicorn
  never configures the root logger, so those records were silently
  dropped — only WARNINGs surfaced, bare, via the stdlib last-resort
  handler. Text mode now installs a formatted stdout handler on the root
  logger when nothing else has configured it (`basicConfig` semantics —
  gunicorn's logger class or an operator script keeps full control),
  rendering the record's structured extras as `key=value` pairs (the
  text-mode mirror of the JSON formatter). One console line per request,
  greppable, no config needed.
- Authentication and authorization rejections were invisible server-side:
  uvicorn's access log shows only the bare `401`, and failed auth never
  reaches the request-log pipeline (there is no key to log against) —
  only the aggregate `gateway_auth_total{result="rejected"}` counter
  recorded them. Every rejection now logs one WARNING line on
  `gateway.auth`: method, path, client address, the client-facing
  reason (the same message the 401/403 body carries) and a redacted
  12-character credential hint (`key_prefix` shape — never the full
  key). Scope rejections (IP allowlist, endpoint scopes) name the key.
- Provider credentials (and `OTEL_*` endpoints) in `.env` were silently
  ignored: pydantic-settings reads the file into the `Settings` model
  only (the `GATEWAY_*` fields), while provider keys are resolved from
  the process environment (`api_key_env`, e.g. `OPENAI_API_KEY`) — so
  the documented quick start sent empty credentials to providers.
  `get_settings()` now exports the dotenv file into the environment
  first (real environment variables win; empty values count as unset;
  a missing file is a no-op). Tests construct `Settings(...)`
  directly and stay hermetic.
- `config.yaml`: the `glm-53` model's Parasail deployment sent
  `model: glm-53`, which that API does not serve (every request fell
  through to the Anthropic fallback). The provider-native id is
  `parasail-glm-53`.
- Budget alerts racing: two requests crossing thresholds concurrently
  (two keys pooling onto one tenant, or two requests on one key) each
  evaluate from a spend snapshot that predates the other's alert write,
  so both saw the same stale fired-levels — a level could deliver
  twice and the loser's write could clobber the winner's persisted
  set. The fired-levels update is now a compare-and-swap on the stored
  value; a losing writer re-reads the winner's state and fires only
  what it missed. Regression tests pin exactly-once delivery for both
  the key and tenant kinds.

### Added

- PostgreSQL support: the `postgres` extra (`asyncpg`) plus
  `GATEWAY_DATABASE_URL=postgresql+asyncpg://...` runs the same
  gateway on PostgreSQL. JSON columns compile to JSONB there via a
  shared variant type — which also keeps the budget-alert
  compare-and-swap working (plain `json` has no equality operator) —
  and pooled connections are pre-pinged so server-side drops recycle
  instead of surfacing as 500s. The entire test suite runs against
  either backend: `GATEWAY_TEST_DATABASE_URL` opts the suite into a
  real PostgreSQL server, giving every test its own database (created
  and dropped around the run). Online backups (`POST /admin/backup`)
  stay SQLite-only: any other backend is refused with a 400
  (`backup_not_supported`) pointing at the platform's own tooling
  (`pg_dump`) — a half-copy through a different mechanism would be a
  false sense of safety.
- Tenants in the admin dashboard: a Tenants tab (platform admins
  only — a tenant admin lands on a note pointing at their scoped tabs)
  with the tenant table (state incl. kill-switch badge, key count,
  model allowlist, pooled RPM/TPM, the same budget bars as keys),
  create/edit/delete via dialog (delete surfaces the 409-while-referenced
  refusal). The key dialog gains tenant placement (platform admins;
  edits re-home, including out of a tenant) and an admin-key checkbox
  (creation only — it is not patchable), and the keys table badges
  tenant membership and admin keys. The overview gains a by-tenant
  breakdown when tenants have traffic. The dashboard learns which kind
  of administrator signed in by probing `/admin/tenants` once.
- CI workflow (`.github/workflows/ci.yml`): the full test suite on
  Python 3.12–3.14 via uv (cached) against SQLite, plus a Python 3.12
  job running the same suite against a PostgreSQL 17 service
  container, and a smoke-check of the shipped `config.yaml`. Dormant
  until the repository is pushed to GitHub.

## [1.0.0] — 2026-11-12

First stable release. Since 0.1.0: images, audio and multimodal chat —
the non-chat modalities on the same policy spine (keys, routing,
retries, failover, breakers, budgets, rate limits, logging), plus audio
input and image output inside chat, audio output in chat, and native
Gemini embeddings inbound; and multi-tenancy — tenants (organizations)
with a pooled, narrowing policy ceiling over groups of virtual keys,
tenant admins scoped to their own tenant, and tenant-isolated caches.
732 tests, no network required.

### Added

- Multi-tenancy: `POST /admin/tenants`, `GET /admin/tenants`,
  `GET/PATCH/DELETE /admin/tenants/{id}` (platform admins). A tenant
  groups virtual keys under a shared policy ceiling — model allowlist,
  RPM/TPM, total/daily/monthly budgets, the same budget machinery as
  keys but pooled across every member. Strictly narrowing: a member key
  must pass both its own checks and the tenant's; there is **no admin
  bypass at the tenant layer** (a tenant admin is still a member). The
  slug is validated header-safe (`^[A-Za-z0-9][A-Za-z0-9_-]*$`, it
  surfaces in `X-Gateway-Tenant` and log filters); delete is refused
  with 409 while any key still references the tenant.
- Tenant admins: `POST /admin/keys` gains `is_admin` (admin rights;
  inside a tenant that means a tenant admin — not patchable later) and
  `tenant_id` (placement, platform admins only: tenant admins' creates
  are forced into their own tenant and a mismatching explicit value is
  a 400). Tenant admins manage their own tenant's keys and read its
  logs/stats/usage; cross-tenant ids answer 404 (no existence leaks);
  the platform's config endpoints (tenants, prompts, routing,
  guardrails, reload, backups) answer 403 via `require_platform_admin`;
  re-homing keys between tenants is platform-admin only (403 for tenant
  admins, unknown target 400).
- Tenant enforcement on every inbound surface (chat, native
  Anthropic/Gemini, the Responses bridge, embeddings, images, audio):
  a second pooled rate-limit window (`tenant:{id}` namespace, TPM
  included), pooled budget checks on all three windows (429
  `insufficient_quota` naming the tenant), and the tenant's model
  allowlist applied to every member key (404 like the key's own).
  Data-plane responses carry `X-Gateway-Tenant`.
- Tenant fail-closed auth: a dangling `tenant_id` (corrupt state) or a
  disabled tenant (`is_active: false` — the kill switch, re-enable
  restores) rejects every member key with 401.
- Pooled spend + tenant budget alerts: every member request accumulates
  on both the key row and the tenant row (separate transactions, so a
  tenant-write failure never loses the key's accounting); alerts fire
  per level on either, with `kind: "key" | "tenant"` in the webhook
  payload and fired levels persisted on the matching row; PATCHing a
  tenant's budget re-arms its alerts.
- Tenancy observability: log rows and the usage export carry
  `tenant_id` (CSV column after `key_name`, empty for platform keys),
  `/admin/logs?tenant_id=` filter (forced for tenant admins), and
  `by_tenant` breakdowns in `/admin/stats` and the JSON usage export
  (platform admins).
- Audit records for tenant created/updated/deleted.
- `POST /v1/images/generations` — OpenAI Images API. OpenAI/Azure pass
  through; Gemini translates to `generateContent` with image output
  (`n` images = `n` sequential calls; `size`/`quality` have no Gemini
  equivalent and are ignored). `stream: true` is rejected with a 400.
- `POST /v1/images/edits` — OpenAI image edits (multipart: `image` as a
  file, repeated `image[]` files, or a base64 string with optional
  `data:` URL prefix; `prompt`; optional `mask`). OpenAI/Azure pass
  through; Gemini translates to `generateContent` with the uploads as
  inline input parts (`mask` has no equivalent there).
- `POST /v1/images/variations` — DALL·E 2 variations (multipart `image`,
  no prompt; OpenAI/Azure only).
- `POST /v1/audio/transcriptions` + `POST /v1/audio/translations` —
  multipart, parsed once at the edge, re-serialized upstream with unknown
  form fields passed through. JSON (`json`/`verbose_json`) or text
  (`text`/`srt`/`vtt`) responses per `response_format`. Uploads capped by
  `uploads.max_upload_mb` (default 25 MB, hot-reloadable; 413 beyond).
- `POST /v1/audio/speech` — OpenAI Speech API, **streamed**: audio
  forwards as the provider sends it; cost/metrics/logging land when the
  body completes (spend survives client disconnects).
- Gemini TTS (`gemini-2.5-*-preview-tts` via `generateContent`, AUDIO
  modality): OpenAI voice names map to Gemini prebuilt voices
  (approximate, deterministic pairing; unknown names pass through so
  Gemini voice names work directly); raw PCM output is served wrapped in
  a WAV header (`response_format: pcm` serves it raw) — mp3 & friends
  are not transcoded; `speed` has no equivalent and is ignored.
- `ProviderAdapter` contract extended: `generate_image` / `edit_image` /
  `vary_image` / `transcribe` / `speak`, each defaulting to a
  501-with-fallback so chat-only providers (Anthropic) need no special
  handling — the routing group falls through. The router's non-streaming
  executor is now one shared `_dispatch` behind all five modality entry
  points.
- Non-token cost tracking: unit prices in the `pricing:` block
  (`image_usd` per image, `audio_usd_per_minute` — billed from the
  duration `verbose_json` reports, `speech_usd_per_1m_chars`), same
  longest-prefix match as token prices.
- Endpoint scopes `images` and `audio` for per-key `allowed_endpoints`.
- Uploads are never persisted: log rows store file names + sizes; speech
  responses are logged as a note (media type + byte count).
- Audio input in chat (`input_audio` parts) on every inbound format:
  OpenAI chat and the Responses bridge (`input_audio` input items) pass
  them to OpenAI/Azure verbatim; native Gemini `inlineData` audio
  translates to `input_audio` (and back). The Anthropic adapter refuses
  audio input with a clear 400 (`audio_input_not_supported`) and
  `fallback=True`, so mixed routing groups still serve it elsewhere.
  Guardrails and token estimation see an `[audio]` placeholder, never the
  bytes.
- Image-to-image chat: Gemini image models (e.g. `gemini-2.5-flash-image`)
  on the chat surface get `responseModalities: TEXT+IMAGE` automatically
  (models with `image` in the name); generated images return as
  `image_url` content parts in `message.content`, rendered back to
  `inlineData` on the native Gemini surface. Streaming is refused with a
  clear 400 (an image has no chunk-delta representation; retry with
  `stream: false`).
- Audio output in chat: `modalities: ["text", "audio"]` + `audio:
  {voice, format}` (gpt-4o-audio shape) return `message.audio`
  (`transcript` + base64 WAV `data`, or raw `pcm16`), streamed as
  `delta.audio` chunks. OpenAI/Azure pass both fields through verbatim;
  Gemini translates to `responseModalities: [TEXT, AUDIO]` +
  `speechConfig` (voice via the TTS voice map) and WAV-wraps the reply's
  PCM; native Gemini inbound accepts the same request through
  `generationConfig` and renders the reply back as text + `inlineData`
  (RIFF-sniffed). Anthropic refuses audio output with a clear 400
  (`audio_output_not_supported`, `fallback=True`); the Anthropic-native
  surface renders an audio reply's transcript as its text block.
  Audio-output chat does not stream on Gemini (`audio_streaming_not_supported`
  — retry with `stream: false`). Guardrails see the words, never the
  bytes: response checks flatten `message.audio.transcript`
  (`utils.assistant_text`), streamed `delta.audio.transcript` deltas feed
  the stream-phase checks, and the PII redactors redact transcript text
  (audio bytes are never rewritten). The Responses bridge renders an
  audio reply's transcript as `output_text`.
- Native Gemini embeddings inbound: `POST /v1[/beta]/models/{model}:embedContent`
  and `:batchEmbedContents` run the same embeddings pipeline (allowlists,
  rate limits, budgets, caching, retries/failover, logs) and render
  replies as `{embedding: {values}}` / `{embeddings: [{values}]}`. The
  paths carry the `embeddings` token scope. `taskType`/`title` and
  batch sub-requests with mixed `model`s or `outputDimensionality` are
  refused with clear 400s (they change the result — fail loudly);
  `outputDimensionality` maps to `dimensions`.

### Changed

- Response-cache identity is now policy-salted: the exact and semantic
  chat caches fold in a **response-policy token** (the tenant plus every
  response-capable guardrail active for the caller — so `disabled_guardrails`
  and guardrail scoping split entries), and the embeddings cache is salted
  by tenant alone. Entries never cross tenants or policies; keys with
  identical policies still share. (Correctness fix: previously a caller
  whose response guardrails differed — e.g. a redactor disabled — could be
  served an entry stored under the other policy, skipping guardrails.)
- Key-level model-allowlist enforcement moved inside
  `_check_model_permission`; the bypass is now precisely "platform admin"
  (`is_admin` with no tenant) — tenant admins pass their tenant's
  allowlist like every member.
- `handle_embedding` and the new modality handlers share one pipeline
  wrapper (`_serve_simple`); embeddings behavior is unchanged.
- The multipart upload cap covers image-edit/variation uploads too
  (`uploads.max_upload_mb`, replacing the audio-only knob of the
  unreleased Tier 10 build).

## [0.1.0] — 2026-11-12

First tagged milestone: a production-shaped LLM gateway with flexible
guardrails, four providers, four inbound wire formats, and a full
operations story. 604 tests, no network required.

### Added

**Proxy & formats**
- OpenAI-compatible `POST /v1/chat/completions` (JSON + SSE streaming) as the
  primary surface — same request/response schema clients already send.
- Native inbound formats, same keys/policy/routing/caching: Anthropic
  Messages (`POST /v1/messages`), Gemini (`/v1/models/{model}:generateContent`,
  `:streamGenerateContent` on `/v1` and `/v1beta`), and the OpenAI Responses
  API (`POST /v1/responses`, a stateless bridge covering the chat-convertible
  subset — the OpenAI Agents SDK works against it).
- Southbound providers: OpenAI (passthrough), Azure OpenAI (deployment URLs,
  `api-version`), Anthropic and Gemini (full request/response/stream
  translation incl. tools, images, sampling params). Native southbound
  Responses for OpenAI/Azure (`deployment.native_responses`).
- `POST /v1/embeddings` (OpenAI passthrough, Gemini `batchEmbedContents`
  incl. >100-input chunking, Anthropic 501) + `GET /v1/models`.
- Vision input (image parts) across all four inbound formats.

**Keys & policy**
- Virtual keys: hashed at rest, per-key RPM/TPM rate limits, total/daily/
  monthly budgets with threshold webhook alerts, model allowlists (fnmatch),
  endpoint/IP scoping, per-key model mappings and default model, prompt
  policy, `disabled_guardrails` runtime override.
- Pluggable auth chain (`admin_key` / `virtual_key` / `jwt` incl. JWKS /
  custom `"pkg.module:Class"` steps); JWT callers adopt the named key's policy.
- Guardrails: pluggable `Guardrail` ABC with four phases (request / response /
  stream-block / stream-rewrite), reject-and-redirect (allowlist re-checked,
  fail closed), arbitrary request-field rewrites, mid-stream text rewriting
  (reserve-based; PII masked as it streams), per-key/model/route scoping
  (fnmatch globs), observe mode. Built-ins: PII redaction (incl. Presidio
  extra), prompt-injection scoring, keyword blocklist, output moderation,
  response schema, request limits.

**Routing & resilience**
- YAML topology: models → deployments with priority tiers, weights, shadows;
  `model_aliases` / `default_model`; retries with backoff; cross-provider
  failover; per-deployment circuit breakers (in-memory or Redis-shared).
- Shadow deployments (mirror traffic to candidates) and prompt A/B mirroring
  from the prompt library — background, never client-facing.

**Caching**
- Exact-match response cache (JSON + stream replay) and opt-in semantic cache
  (embedding similarity, same-model isolation); in-memory or Redis backends.

**Prompt ops**
- Managed system prompts (global or per-key; client-supplied always wins),
  `{{variable}}` templating, and a versioned prompt library with
  activation/rollback and candidate A/B — admin API + UI, no restarts.

**Observability & admin**
- Admin API + self-contained dashboard UI (`/admin/ui`): keys, logs, routing/
  breaker state, guardrails, usage export (CSV/JSON), prompts.
- Request logs with guardrail verdicts, tokens, cost, latency, trace ids;
  PII-safe mode (`store_bodies: false`); retention pruning; opt-in background
  write queue. Prometheus metrics (incl. per-key series), OpenTelemetry
  tracing with W3C propagation, audit webhooks, budget alert webhooks, and a
  full self-hosted stack (collector/Jaeger/Loki/Prometheus/Grafana) in
  `deploy/`.

**Operations**
- Liveness (`/healthz`) separate from readiness (`/readyz` — 503 while
  starting, draining, or DB-down).
- Config hot-reload (`POST /admin/reload` or SIGHUP): guardrails, models,
  routing, pricing and prompt knobs swap in place; invalid config is rejected
  with the running one untouched; restart-required fields reported honestly.
- SQLite backups via online `VACUUM INTO` (`POST /admin/backup`, list/delete).
- Graceful shutdown: readiness drops first, in-flight requests finish,
  tracked background work drains before the engine closes.
- Hardened Docker image (non-root, HEALTHCHECK on readiness) + standalone
  `deploy/docker-compose.yml` (volumes, optional Redis profile).

[1.0.0]: https://example.com/llm-gateway/releases/tag/v1.0.0
[0.1.0]: https://example.com/llm-gateway/releases/tag/v0.1.0
