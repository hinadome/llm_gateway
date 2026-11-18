# Changelog

All notable changes to the LLM Gateway are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

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
