# LLM Gateway

An **OpenAI-compatible LLM gateway** that sits between your applications and LLM providers (OpenAI, Anthropic, Google Gemini, and any OpenAI-compatible API). One endpoint, one schema, centralized control.

```
                          ┌────────────────────────────────────────────────┐
                          │                  LLM Gateway                    │
                          │                                                │
 clients (OpenAI SDK,     │  auth ─ rate limits ─ budgets ─ guardrails ─    │
 LangChain, curl, ...) ──▶│  cache ─ routing (retries/fallbacks) ─         │──▶ OpenAI
                          │  translation ─ logging ─ cost tracking         │──▶ Anthropic
                          │                                                │──▶ Gemini
                          └────────────────────────────────────────────────┘
```

From the client's point of view it *is* the OpenAI API — same request schema, same response schema, same streaming SSE protocol — while the gateway centralizes provider credentials, policies and observability.

## Features

| Area | What you get |
|---|---|
| **OpenAI-compatible API** | `POST /v1/chat/completions` (streaming + non-streaming), `POST /v1/embeddings` (OpenAI + Gemini, exact-match cached), `GET /v1/models` + `GET /v1/models/{model}`, OpenAI-style error envelopes, opt-in CORS for browser clients |
| **Images & audio** | `POST /v1/images/generations`, `/v1/images/edits` (uploaded image(s) + prompt, optional mask; base64 string input also accepted) and `/v1/images/variations`; `POST /v1/audio/transcriptions` + `/v1/audio/translations` (multipart, unknown form fields passed through); `POST /v1/audio/speech` — **streamed** as the provider sends it. OpenAI/Azure pass through; Gemini translates image generation and editing to `generateContent`, and speaks TTS natively (voice mapping, WAV output). Same keys, routing, retries, failover, breakers, budgets, rate limits and logs as chat; cost tracked per image / per minute / per 1M chars |
| **Multimodal chat** | Audio input in chat (`input_audio` parts) on every inbound format — OpenAI chat, the Responses bridge, and native Gemini (`inlineData` audio) — with the full policy pipeline (guardrails see an `[audio]` placeholder, never the bytes). **Audio output**: `modalities: ["text", "audio"]` + `audio: {voice, format}` returns `message.audio` (transcript + base64 WAV, or raw `pcm16`) — OpenAI/Azure passthrough incl. streamed `delta.audio`; Gemini translates to `responseModalities` + `speechConfig` (voice map shared with TTS); guardrails see transcripts (block/redact text, never the bytes). Image-to-image chat: Gemini image models (`gemini-2.5-flash-image`) on the chat surface return generated images as `image_url` content parts, round-tripped through the native Gemini format |
| **Native inbound formats** | Anthropic Messages API (`POST /v1/messages`) and Gemini format (`POST /v1/models/{model}:generateContent`, `:streamGenerateContent`, `:embedContent`, `:batchEmbedContents` — `/v1` and `/v1beta`) accepted natively: same keys, guardrails, budgets, routing and caching as the OpenAI surface, responses translated back to each format (verified against the real `anthropic` and `google-genai` SDKs) |
| **Responses API bridge** | `POST /v1/responses` (the OpenAI Agents SDK's default transport) translated to the chat-completions core — input items, instructions, function tools, `text.format`, streaming SSE events; verified against the real OpenAI SDK and OpenAI Agents SDK (see [Agent frameworks](#agent-frameworks)). Deployments can additionally set `native_responses: true` to forward those requests to OpenAI's `/responses` endpoint **in their original shape** (see below) |
| **Realtime API** | `WS /v1/realtime?model=<public-model>` — the fifth inbound surface and the first stateful one: one long-lived bidirectional event session instead of request/response. The open gates (auth incl. `?key=`, the `realtime` endpoint scope, model permission, RPM at open, budget, a concurrency slot held for the session's life) run accept-first with OpenAI-shaped realtime error events + close 1008; routing walks the residency/breaker-filtered ladder at **connect time only** (long-lived sessions cannot fail over mid-stream) with the BYOK vault on the handshake. Per-event guardrails: request phase over each `response.create` (over its input, or the shadowed conversation when input is absent), stream phase + held-back rewriting over `output_text.delta` (a mid-response block cancels upstream, suppresses the rest of that response, and its `response.done` still bills). One request-log row per session (tokens/cost summed across responses, TTFT on the first delta), `gateway_realtime_sessions{provider,model}` (see [Realtime API](#realtime-api)) |
| **MCP proxy** | `POST /v1/mcp/{server}` — the sixth inbound surface, a transparent **Model Context Protocol** Streamable-HTTP proxy (2025-06-18 transport) between MCP clients and operator-configured MCP servers: same keys/auth (incl. `?key=`), a gateway-issued `Mcp-Session-Id` mapped to the upstream session (a client id is useless upstream), the tier-30 tool policy applied per tool name (`tools/list` filtered to what the key may actually call, `tools/call` admitted one name at a time, denials in-band as JSON-RPC errors), request/response guardrails over **every content-bearing method** — tool arguments/result texts, embedded-resource texts and `structuredContent` on `tools/call`, plus the whole params and every server text shape on `resources/read` / `prompts/get` / `completion/complete` (rewrites applied only while the JSON stays a valid object — otherwise fail closed; base64 payloads skipped by design; guardrail `scope:` binds with `models:` matching the *server* name), `allowed_mcp_servers` allowlists (key + tenant, fnmatch; `mcp` endpoint scope; RPM — no TPM/budget, no tokens exist), SSE answers relayed live with the response frame intercepted, DELETE session termination + TTL sweep, one log row per message and `gateway_mcp_requests_total{server,method,outcome}` (see [MCP proxy](#mcp-proxy)) |
| **Guardrails (in/out)** | Pluggable request/response/stream-phase checks: PII redaction (regex or Microsoft Presidio), prompt-injection heuristics, keyword blocklists, output moderation, response-schema validation, request limits, context-window management, **prompt compression** (middle-out folding of over-budget prompts to fit — the deterministic, no-LLM-call remediation counterpart of context-window blocking; observe mode measures the savings before you enforce). Request-phase guardrails can **modify** more than messages — rewrite `model` (reject-and-redirect: "too expensive for the flagship model, serve it on the small one"), sampling params and tools, with modifications **chaining** across guardrails. Stream-phase guardrails can **rewrite output as it streams** (PII masked mid-stream, never reaching the client, no matter how the provider chunked it). Any guardrail can run in **observe mode** (log & count would-be verdicts without enforcing) and can be **scoped** to certain keys/models/routes (`scope` with fnmatch globs; per-route policy like "strict checks on `/v1/chat/completions`, looser on the native formats"; per-key runtime overrides via `disabled_guardrails`) |
| **Model aliasing** | `model_aliases` maps client-requested names to your public model names — apps with hardcoded provider model names (`gpt-4o`, `claude-3-5-sonnet-...`) work unchanged; opt-in `default_model` catches any unknown name; real model names always win over aliases |
| **Per-key model routing** | Each virtual key can carry its own `model_mappings` (requested name → public model) and `default_model` — per-team steering without code changes. Mappings apply before global aliases and win even over real public names; everything downstream (permissions, budgets, caches, logs) sees the resolved name |
| **Managed system prompts** | A global `prompts.system_prompt` or a per-key `system_prompt` (admin API/UI, no restart) is prepended to requests that carry **no system message of their own** — the gateway manages the prompt for callers that don't provide one; a client-supplied system message (OpenAI `system`/`developer`, Anthropic `system`, Gemini `systemInstruction`, Responses `instructions`) always wins. Injection runs before guardrails and cache lookup, so the prompt that goes out is the prompt that gets checked and cached |
| **Prompt templating** | `{{variable}}` placeholders in message text (and in the managed system prompt) resolve against `prompts.variables` merged with the key's `prompt_variables` (key wins) — one persona template, different values per team. Only *known* names are replaced: unknown placeholders and literal braces (code snippets, JSON examples) pass through untouched |
| **Prompt library (prompt ops)** | Versioned named prompts in the DB (`POST /admin/prompts` + the admin UI's Prompts tab): a key can reference one by name (`system_prompt_ref`) instead of inline text — its **active** version serves, so editing a prompt is a version bump + activate (no key change, no restart, instant rollback). A second **candidate** version A/B-mirrors in the background (a fraction of successful traffic, own log rows flagged with the variant, `gateway_prompt_mirror_*` metrics — never client-facing, never in stats or spend) |
| **Inspection** | Every request/response stored with guardrail decisions, tokens, cost, latency; queryable via `/admin/logs` (filterable by trace id); Prometheus metrics at `/metrics`; OpenTelemetry tracing + logs with trace correlation (opt-in); full Grafana/collector stack in `deploy/`; PII-safe mode stores no bodies (`store_bodies: false`) and old rows are pruned automatically (`retention_days`) |
| **Admin UI** | Built-in dashboard at `/admin/ui`: key management (create/edit/**rotate/revoke**, budget bars, alert badges, tenant placement, admin-key grants), tenant management (create/edit/delete with pooled budget bars and the kill switch; platform admins only — tenant admins land on a scoped note), request-log browsing with full request/response inspection, routing table with live circuit-breaker state, guardrail inventory, a **Playground tab** (tier 41: send a chat through the real pipeline as the admin or any key — model/key dropdowns, multi-turn conversation, and the request-log row's diagnostics beside it; see [Admin playground](#admin-playground)), and a System tab (health, config reload, backups) — one self-contained HTML file, no build step, no external resources (works offline) |
| **Virtual keys** | Per-key model allowlists (fnmatch patterns), **tool allowlists** (`allowed_tools`, fnmatch patterns over the tool *names* a request may declare — see [Design notes](#design-notes--limitations-mvp)), **MCP server allowlists** (`allowed_mcp_servers`, fnmatch patterns over the proxied server names — see [MCP proxy](#mcp-proxy)), RPM/TPM rate limits, **in-flight concurrency caps with priority queueing** (QoS: max simultaneous upstream requests per key — cache hits and idempotent replays bypass; waiters queue highest-priority-first, then 429 + `Retry-After`), USD budgets with spend tracking — total plus **daily / monthly windows** (UTC calendar caps, e.g. "no more than $5/day"), an opt-in **projected stance** (`budget_enforcement:`) that denies on the month-end run-rate trend before the money is spent, and **credential rotation with a grace window** — `POST /admin/keys/{id}/rotate` replaces the secret while the row keeps its identity, the old credential working until its window ends (see [Key rotation & grace windows](#key-rotation--grace-windows)) |
| **Multi-tenancy** | Tenants (organizations) group virtual keys under a shared policy ceiling — model allowlist, RPM/TPM, and total/daily/monthly budgets that apply to *every* member key, tenant admins included (they manage their own tenant's keys, not its policy; a member must pass both its own checks and the tenant's — the tenant can only narrow). **`pricing_markup_pct`** bills member traffic at cost-plus (20.0 = +20% on provider cost): spend, budgets, logs, exports and the per-key metrics record the *billed* cost while `gateway_cost_usd_total{model}` keeps the actual, and `gateway_markup_usd_total{tenant}` counts the revenue (see [Tenant pricing markup](#tenant-pricing-markup)). **Tenant admins** (`is_admin` key inside a tenant) see only their tenant's keys/logs/stats/usage (a cross-tenant id is a 404, not a 403 — no existence leaks) and are 403 on platform endpoints (tenants, prompts, routing, guardrails, reload, backups). `is_active: false` is a kill switch for every member key (re-enable restores them); pooled spend and budget alerts fire per tenant too (`kind: "tenant"` in the payload); responses carry `X-Gateway-Tenant`; caches are isolated per tenant |
| **BYOK vault** | Tenants bring their **own provider credentials**: store one encrypted key per (tenant, provider) — `PUT /admin/tenants/{id}/provider-keys/{provider}` — and every request from that tenant rides *their* provider account instead of the platform's configured key, through every retry and fallback leg (per-candidate resolution; with `vault.strict: true` the ladder only ever contains providers the tenant keyed — an unkeyed model refuses 403 `byok_key_required`). Fernet-encrypted at rest under `GATEWAY_VAULT_MASTER_KEY` (MultiFernet list = rotation), the raw key exists only in the PUT body and the upstream auth header — the API speaks a hint (`…1234`), audit records the same, log rows record only the provider name (`byok_provider`) plus `X-Gateway-Byok-Provider` and `gateway_byok_requests_total`. Undecryptable entries fail closed (`byok_key_unreadable` — never a silent platform-key fallback); without a master key the vault is off and every caller is served exactly as before. `vault.backend: redis` fans admin-write invalidations out to every replica (the TTL is the backstop); the admin UI's Vault tab manages credentials for both admin kinds (see [BYOK vault](#byok-vault)) |
| **Pluggable auth chain** | `auth.chain` lists the strategies that may authenticate a caller, in order: built-in `admin_key` / `virtual_key` / `jwt`, or your own `"pkg.module:ClassName"` step. A **JWT/OAuth bearer token** authenticates the virtual key named by its `sub` claim (HS256 secret or a JWKS with rotation) — budgets, limits and spend accrue to that key exactly as its raw key would |
| **Audit log** | Security-relevant admin actions (key create/update/rotate/revoke, breaker reset) POSTed as JSON webhooks to your SIEM/audit trail — who (key + auth method), what, when; best-effort background delivery, counted on `gateway_audit_events_total` |
| **Token scoping** | Per-key `allowed_endpoints` (`chat` / `embeddings` / `images` / `audio` / `models` / `realtime` / `mcp`; `chat` covers the native formats and the Responses bridge) and `allowed_ips` (plain IPs or CIDR blocks) — defense in depth beyond model allowlists; admins are never scoped |
| **Budget alerts** | Webhook fired when a key's spend crosses a threshold of its budget (default 50/80/90/100%, configurable); once per level per key, survives restarts, raising the budget re-arms; best-effort delivery with retry, counted on `gateway_alerts_total` |
| **Key-expiry warnings** | The deadline's own pages: a background sweep (hourly, `alerts.expiry_days` — default 14/7/1/0; **0 = the page that the key just died**) fires the same webhook + an `expiry.threshold` event once per day-level as a key's `expires_at` approaches, highest first. Fired levels persist on the key row (a restart never re-pages); setting or moving a deadline **arms silently** — the operator just chose it — while a key the sweep *discovers* near its deadline fires honestly; revoked and deadline-less keys never warn; rotation never touches the state. Spend-triggered alerts cannot see a quiet key — this one does (see [Key expiry warnings](#key-expiry-warnings)) |
| **Routing** | Weighted load balancing, retries with exponential backoff, cross-provider fallbacks, per-deployment circuit breakers (failing deployments are skipped, probed after a cooldown; state visible in `/admin/routing` and on `gateway_circuit_state`; per-process by default, or **shared across replicas via Redis** with `routing.breaker.backend: redis`). **Canary** = a deployment's `weight` within a tier (raise it gradually to shift traffic); **shadow** = mirror a fraction of successful requests to a non-serving deployment for comparison — every mirror is scored raw-vs-raw into `exact`/`near`/`divergent` verdicts with similarity, latency and cost deltas, and `/admin/shadow-diff` turns the pairs into per-deployment migration evidence (see [Shadow diffing](#shadow-diffing)); **trials** = deliberately replay a captured window against any candidate deployment with real upstream calls — throttled, spend-capped, scored the same way (see [Candidate trials](#candidate-trials)); **verdicts** = the go/no-go recommendation over either evidence leg — go / hold / no_go / insufficient_evidence with per-check numbers, thresholds hot-reloadable (see [Migration verdicts](#migration-verdicts)); **promotion** = apply the go verdict through config.yaml itself — plan renders the exact edits, promote backs up, writes, hot-reloads and audits; a no_go blocks unless forced, nothing is ever auto-removed (see [Migration promotion](#migration-promotion)); **rollback** = the undo surface over the backups every apply writes — list each with the changes restoring it would make, preview the diff, restore through the same backup-first → hot-reload → audit discipline (see [Promotion rollback](#promotion-rollback)); **watch** = the post-promotion leg — serving traffic re-verdicted against the same thresholds from the moment each deployment started serving, a recommendation to keep serving or roll back, never an automatic one (see [Post-promotion watch](#post-promotion-watch)); **cost-aware** = rank same-priority deployments by their upstream model's input price — cheapest first, the price ladder doubling as the failover ladder (`routing.cost_aware`); **residency-constrained** = deployment `residency` tags + key/tenant `required_residencies` filter the ladder before anything else (see [Data-residency routing](#data-residency-routing)) |
| **Usage export** | `GET /admin/usage/export` (CSV or JSON): billing / chargeback extract of request rows with cost/tokens/latency, time-window + key/model/status filters, and per-key / per-model / per-status aggregates; mirrored shadow calls excluded by default |
| **Usage invoices** | `GET /admin/invoices`: the month statement the finance side asks for — what each tenant consumed, per key, per model — the usage-export rows rolled up into line items with the key's and tenant's monthly budget beside the spend (a spend-vs-plan check in the same read); zero-usage keys included (an unused key is a finops finding), deleted keys billed where they lived; CSV export one row per line item (see [Billing & usage invoices](#billing-usage-invoices)) |
| **Cost anomaly flags** | `GET /admin/anomalies`: the invoice's follow-up — *is any of this unusual?* The recent window vs each key's trailing baseline, per key and per model over the same client-consumed lens: `cost_spike`, `traffic_spike`, `traffic_stop` (a key gone silent — the churn/broken-integration signal a spend dashboard structurally cannot show) and `first_seen_model`, each carrying its own numbers and a human message; thin baselines are honestly no verdict, zero-cost baselines fire with `ratio: null` — never a fabricated infinity; informational flags, never pager alerts (see [Finops anomaly flags](#finops-anomaly-flags)) |
| **Spend forecasts** | `GET /admin/forecasts`: where will this month land? Run-rate (elapsed-month extrapolation) and recent-pace (last 7 days carried over the rest) projections per key and per tenant, trend ratio beside them, projected-vs-budget verdicts (over / at_risk ≥80% / within) decided by the recent pace when both exist; a complete month serves its actuals (`month_complete: true`), a trend needs 2+ days of month, and the clock context ships in the envelope so every number is auditable (see [Spend forecasts](#spend-forecasts)) |
| **Budget forecast alerts** | `forecast_alerts:` — the projections get teeth: a background evaluator (one pass = the same forecast the dashboard serves) pages a webhook + `forecast.alert` / `forecast.recovered` events when a key's or tenant's month-end projection trends over its budget (`trending_over`) or at risk ≥80% (`trending_at_risk`), and when it provably heals (a budget raise is an honest fix); fire-once per (month, entity) on the settings KV table — a restart never re-pages, a new month is a new plan — the early month gates on `min_elapsed_days` (a day-one straight-line never pages), no budget = no bar (a removed budget drops state silently); `GET /admin/forecasts` carries each section's persisted `alert` state and the config echo (see [Spend forecasts](#spend-forecasts)) |
| **Projected budget enforcement** | `budget_enforcement:` — the gate leg the hard caps lack: with `enabled: true`, a key or tenant whose month-end **run-rate projection** (snapshot spend over elapsed days, scaled to the month — the same counter lens the hard cap reads, zero extra queries) exceeds its `monthly_budget_usd` is denied the same `429 insufficient_quota`, the message saying it is the projection with the numbers; the hard cap still fires first when actual spend has crossed, the denial self-heals (a projection is a ratio — a genuinely stopped caller's projection decays under its budget as the month elapses and traffic resumes), `min_elapsed_days` (default 2.0) keeps day-one straight lines from denying, and the stance is off by default — today's hard-cap-only behavior is exactly preserved (see [Spend forecasts](#spend-forecasts)) |
| **Batch API** | OpenAI-style `/v1/files` + `/v1/batches`: upload a JSONL of chat requests, execute them in the background — each line through the *full* pipeline (guardrails, cache, routing, spend, request logs) — then fetch per-line results as output/error JSONL. Rate limits and budgets defer a batch instead of failing lines; the 24h completion window bounds retries (see [Batch API](#batch-api)) |
| **Lifecycle events** | Subscribe your own handlers to the request-stage boundaries (`request.received` / `routed` / `cache_hit` / `rejected` / `failed`, `response.completed`, `stream.started` / `completed`, `auth.rejected`, `key.*`, `budget.threshold`, `expiry.threshold`, `batch.*`) — **observers** are fire-and-forget on a background queue (a slow or crashing handler can never affect a request), **interceptors** run in-band with veto power at the two pre-irreversible events; with no handlers the bus is inert (see [Lifecycle events](#lifecycle-events)) |
| **Feedback capture** | `POST /v1/feedback` lets clients rate any logged request (by `X-Request-Id`, rating 1–5) — the missing half of the quality story: shadow mirroring and prompt A/B compare *outputs*, feedback says which one was good. Admin list + per-model/per-key quality aggregates; counted on `gateway_feedback_total`; `feedback.recorded` event (see [Feedback](#feedback)) |
| **Synthetic canary probes** | A background loop sends a tiny chat request straight to every configured deployment's provider on a schedule (`canary:` config, disabled by default) — upstream health is *measured*, not inferred from traffic. Direct adapter calls: no pipeline, no log rows, no spend, no breaker accounting. `GET /admin/canaries` fleet view, `POST /admin/canaries/probe` for on-demand waves; `canary.failed` / `canary.recovered` events; `gateway_canary_probes_total{deployment,provider,outcome}` (see [Canary probes](#canary-probes)) |
| **Service level objectives, error budgets & alerting** | `slos:` declares named promises about the client experience — a metric (`error_rate`, latency p50/p95/p99, `ttft_p95_ms`), a ceiling, a rolling window and a sufficiency minimum, scoped to a model or one deployment. `GET /admin/slos` (platform admin) measures each over the request logs — non-shadow, non-trial rows, cache replays included, unattributed errors included at model scope — and verdicts `met` / `breached` / `insufficient_evidence` with the full numbers beside every verdict. Every bar doubles as an error-budget allowance with burn rate and multiwindow `burn:` rules (the SRE anti-flap — a long window establishes the pace, a short one confirms it is *still* burning). A background evaluator (tier 44) turns the same numbers into `slo.alert`/`slo.recovered` transitions — fire-once per target, persisted across restarts, webhook delivery, a quiet window neither fires nor un-fires. Hot-reloadable, measured at read time; the Overview tab renders the panel with a Budget column and an alerting badge. (see [Service level objectives (SLOs)](#service-level-objectives-slos)) |
| **PII/secret scan** | On-demand audit over *stored* request bodies (`GET /admin/pii-scan` bulk, `GET /admin/logs/{id}/pii-scan` per row): the log keeps the original as received, so this is where exposure actually shows — clients paste API keys into prompts; guardrails may be off or only masking the upstream copy. Detectors mirror the `pii_redaction` guardrail plus jwt/private-key shapes, extendable via `pii_scan.extra_patterns`; findings are kind + count only, never the matched text (see [PII scan](#pii-scan)) |
| **Idempotency keys** | Retry-safe chat completions: send an `Idempotency-Key` header on any chat-family surface (OpenAI chat, `/v1/responses`, native Anthropic/Gemini) and retried POSTs replay the stored successful response instead of re-executing — no double generation, no double billing (replays are zero-cost `cache_hit` log rows, `X-Gateway-Cache: IDEMPOTENT`). Only successes are stored; key reuse with different parameters is a 400; in-flight duplicates wait for the original. Same backend selection as the response cache, namespaced apart (see [Idempotency](#idempotency)) |
| **Capture-replay harness** | Config-change safety net built on the request log: `GET /admin/logs/export` captures real traffic as JSONL (bodies + the recorded baseline: caller, route, model, status, deployment, guardrail verdicts), `POST /admin/replay` re-executes those requests through the request-policy phases ONLY — resolution, prompt policy, permissions, guardrails, routing selection — with no upstream calls, no counters, no spend, no log rows, then **diffs** each line against the record. Replays run as the recorded caller's *current* key (edited/revoked keys replay under what they are *now* — a revoked key shows up as would-now-401), guardrail counters are suppressed so a harness run can never trip block alerts, and tenant admins replay only their own tenant's traffic (see [Capture-replay harness](#capture-replay-harness)) |
| **Runaway-agent protection** | Identical-request loop detection with an escalating cooldown jail: an agent stuck in a retry/tool loop sends the *same* request over and over — every one individually legal, so RPM/TPM can't see it. The fingerprint (the cache's normalized request identity — stable across chat/Responses/native routes, stream or not) repeated `threshold` times inside the window trips, and the *key* answers 429 + `Retry-After` for everything until the cooldown expires — would-be cache hits and idempotent replays included. Re-trips escalate (30s → 60s → ... capped), a quiet period decays the offense count, `GET /admin/runaway` shows the jail and `POST /admin/runaway/{key_id}/release` is the escape hatch; `runaway.trip` event + `gateway_runaway_trips_total{key}` (see [Runaway-agent protection](#runaway-agent-protection)) |
| **Concurrency admission control & QoS** | Per-key and pooled per-tenant `max_concurrency` caps bound how many requests may be **in-flight against the upstream at once** — RPM/TPM bound the rate, this bounds the parallelism (protects a provider quota or a small deployment fleet). The slot wraps the upstream call: cache hits and idempotent replays never hold one, a streamed response holds it until its body completes, and every error path releases it. At the cap, requests **queue** (no extra upstream pressure) admitted **highest-`priority`-first** (the key's QoS class, FIFO within a class; a waiter on one key never blocks behind an unrelated saturated one); a wait past `concurrency.wait_seconds` answers 429 + `Retry-After` (`0` = fail fast). `GET /admin/concurrency` shows live in-flight/queued counts; `gateway_concurrency_inflight{key}` / `gateway_concurrency_queued{key}` / `gateway_concurrency_rejected_total{key}` + a `concurrency.timeout` event (see [Concurrency admission control](#concurrency-admission-control)) |
| **Data-residency routing** | Deployment-tag-constrained routing, fail closed: each deployment carries an operator-asserted `residency` tag (lowercase slug, e.g. `eu`), and keys/tenants carry `required_residencies` — a caller's traffic (chat, native formats, embeddings, images, audio, *and shadow mirrors*) may only be served by deployments whose tag is in the caller's **effective set** (key ∩ tenant; the tenant can only narrow). The filter runs inside the router **before** the priority ladder, retries and fallbacks, so a constrained request can never escape to a non-compliant deployment mid-failover; an untagged deployment satisfies **no** requirement (no claim, no compliance), and a model with nothing compliant answers 404 `residency_not_satisfied` naming the model and the tags. Cache entries never cross residency classes (the requirement salts the chat response-policy token and the embeddings cache key — unrestricted callers keep their exact pre-existing keys); the semantic cache degrades away rather than embed a restricted prompt non-compliantly (see [Data-residency routing](#data-residency-routing)) |
| **Caching** | Exact-match response cache with TTL; cache hits replay for streaming requests too; optional **semantic cache** serves near-duplicate prompts by embedding similarity (configurable model + threshold) |
| **Provider translation** | OpenAI passthrough (any OpenAI-compatible base: vLLM, Ollama, Groq, ...), Azure OpenAI (deployment-addressed, `api-key` header, `api-version`), Anthropic & Gemini full request/response/stream translation incl. tools |
| **Cost tracking** | Per-1M-token pricing table (configurable), cost per request/key/model — including **provider prompt-cache discounts**: cached input tokens (OpenAI `prompt_tokens_details.cached_tokens`, Anthropic `cache_read_input_tokens`, Gemini `cachedContentTokenCount`) bill at the `cached_input` price, default 50% of input |
| **Background queued logging** | Opt-in `logging.background_write` moves the request-log INSERT off the response path — the last DB round trip a response pays — behind a bounded queue drained by a background consumer; a full queue drops rows (counted on `gateway_logs_dropped_total`) rather than blocking, and shutdown drains everything pending |
| **Per-key metrics** | Opt-in `metrics.per_key` splits request/token/cost counters by virtual key (`gateway_key_*_total`) for per-team dashboards; cardinality capped — keys beyond `per_key_max_series` collapse into one `__overflow__` label, and shadow mirrors are excluded |
| **Operations** | Liveness (`/healthz`) separate from readiness (`/readyz` — 503 while starting, draining on shutdown, or DB-down; circuit-breaker state reported informationally); **config hot-reload** (`POST /admin/reload` or SIGHUP) swaps guardrails/models/routing/pricing without dropping traffic, refuses a bad config, and tells you what needs a restart; **SQLite backups** (`POST /admin/backup` — online `VACUUM INTO`, list/delete via `/admin/backups`; on PostgreSQL the endpoint refuses and points at `pg_dump`); graceful shutdown drains in-flight requests, background spend/shadow work and the log queue; hardened Docker image (non-root, HEALTHCHECK on readiness) + standalone `deploy/docker-compose.yml` (volumes for DB + backups, optional Redis profile) |

## Quick start

```bash
# 1. Install
uv sync

# 2. Configure
cp .env.example .env          # add provider API keys + admin key
# edit config.yaml            # define models, guardrails, routing

# 3. Run
uv run uvicorn gateway.main:app --port 8000
```

On first boot the gateway:
- creates `gateway.db` (SQLite),
- logs a generated **admin key** (or uses `GATEWAY_ADMIN_KEY` from `.env`),
- creates a starter **virtual key** named `default` and logs it once.

Open `http://localhost:8000/admin/ui` and paste the admin key for a live dashboard (keys with tenant placement, tenants, request logs, routing/breaker state, guardrails — auto-refreshing every 5s).

Then point any OpenAI SDK at it:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-gw-...")

resp = client.chat.completions.create(
    model="gpt-4o",  # public name from config.yaml — can fan out to any provider
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Streaming works exactly like OpenAI (`stream=True`, `stream_options={"include_usage": True}`).

## HTTPS

Serve TLS with `python -m gateway` (or its wrapper `scripts/gateway.sh`) and one of three certificate modes — plain HTTP when none is configured:

```bash
./scripts/gateway.sh --self-signed                 # dev: generates deploy/certs/gateway-dev.pem
                                                   # (+ -key.pem) on first run, reuses after
./scripts/gateway.sh --ssl-cert my.crt --ssl-key my.key    # any PEM pair (private/org CA)
./scripts/gateway.sh --letsencrypt api.example.com  # certbot's /etc/letsencrypt/live/... layout
```

- **Self-signed** is for development and internal use: clients must trust the pair explicitly (or skip verification — `curl --cacert deploy/certs/gateway-dev.pem ...`, `OpenAI(http_client=httpx.Client(verify=False))`) because no public CA vouches for it. The certificate covers `localhost`, `127.0.0.1` and `::1`. Keep `deploy/certs/` gitignored — it holds a private key.
- **Private certificate** (`--ssl-cert`/`--ssl-key`, or `GATEWAY_SSL_CERT`/`GATEWAY_SSL_KEY` env vars, read from `.env` too) is the organizational-CA / commercial-certificate path. Both must be set together.
- **Let's Encrypt** resolves certbot's standard paths (`fullchain.pem` + `privkey.pem`); obtain one with `certbot certonly --standalone -d api.example.com`. After certbot renews, restart the gateway — the certificate is read once at bind time, not per connection (the startup log prints the expiry date as a reminder). A nonstandard location can be passed with plain `--ssl-cert`/`--ssl-key`.

**Certbot and root.** The `sudo` in that certbot invocation covers exactly two things: binding port 80 (the `--standalone` challenge server) and writing `/etc/letsencrypt` — root-only because private keys live there. Both are avoidable: a DNS-01 challenge with `--config-dir`/`--work-dir`/`--logs-dir` pointed under a home directory runs fully unprivileged (`--manual` once, or a DNS plugin to automate renewal — also the only route for internal hostnames that aren't publicly routable). One caveat for `--letsencrypt`: certbot's `live`/`archive` directories are `0700 root`, so a gateway running as an unprivileged service user **cannot read `privkey.pem` from the default location**. The production pattern there: a certbot `--deploy-hook` copies the pair to a gateway-readable location on every renewal, and the gateway points `--ssl-cert`/`--ssl-key` at the copies (restart after renewal to load the new pair).

`--host`/`--port` (or `GATEWAY_HOST`/`GATEWAY_PORT`) set the bind address, `--reload` enables code-watch for development, and everything else passes through to the app unchanged. Prefer plain uvicorn? It serves the same app: `uv run uvicorn gateway.main:app --ssl-certfile my.crt --ssl-keyfile my.key`.

With HTTPS on, add `--redirect-http [PORT]` to also listen on plain HTTP (default port 80 — may need `sudo`/`CAP_NET_BIND_SERVICE`) and answer every request there with a **308 Permanent Redirect** to the same URL on HTTPS — browsers and SDKs that default to `http://` get bounced to the encrypted listener instead of a connection error, and POST bodies survive the hop (308 preserves the method).

To terminate TLS in front of the gateway instead (nginx, Caddy, a load balancer), leave it on HTTP — only one TLS layer is needed; clients then send `https://` to the proxy.

## API surface

### Client API (OpenAI-compatible)

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Chat completions, streaming and non-streaming |
| `POST /v1/embeddings` | Embeddings (`float` or `base64` encoding, optional `dimensions`); same keys, routing, caching, cost tracking and logs as chat |
| `GET /v1/models` | Models visible to the calling key (chat + embeddings models, as on OpenAI) |
| `GET /v1/models/{model}` | Model retrieve — frameworks that validate models at startup rely on this |
| `POST /v1/responses` | OpenAI Responses API (the default transport of the OpenAI Agents SDK) — bridged to chat completions; see [Agent frameworks](#agent-frameworks) |
| `WS /v1/realtime?model=` | OpenAI-shaped realtime sessions (WebSocket) — bidirectional event relay with the full policy pipeline; see [Realtime API](#realtime-api) |
| `POST /v1/mcp/{server}` | MCP Streamable-HTTP proxy — one JSON-RPC message per POST (initialize creates a gateway-issued `Mcp-Session-Id`); see [MCP proxy](#mcp-proxy) |
| `DELETE /v1/mcp/{server}` | Terminate an MCP session (with the `Mcp-Session-Id` header) — local drop first, then relayed upstream |
| `GET /v1/mcp/{server}` | 405 — the standalone server→client stream is out of scope (the transport spec permits this) |
| `POST /v1/images/generations` | Image generation — JSON in, JSON out (`data[].b64_json` / `url`); `n`, `size`, `quality`, `style` pass through; `stream: true` is rejected with a 400 |
| `POST /v1/images/edits` | Image editing — multipart: `image` (file, repeated `image[]` files, or base64 string), `prompt`, optional `mask`; unknown fields pass through |
| `POST /v1/images/variations` | Image variations (DALL·E 2 on OpenAI/Azure) — multipart `image`, no prompt |
| `POST /v1/audio/transcriptions` | Audio transcription (multipart: `file` + `model` + optional `language`/`prompt`/`response_format`/`temperature`; unknown fields ride along). JSON or plain-text response per `response_format` |
| `POST /v1/audio/translations` | Audio translation to English — same multipart shape and response formats |
| `POST /v1/audio/speech` | Text-to-speech — JSON in, **streamed** binary audio out (`response_format` selects the codec) |
| `POST /v1/files` | Batch API file upload (multipart: `file` + `purpose: "batch"`, JSONL chat requests) — see [Batch API](#batch-api) |
| `GET /v1/files` | List the calling key's files (cursor-paginated: `limit`, `after`) |
| `GET /v1/files/{id}` | File metadata (OpenAI file-object shape) |
| `GET /v1/files/{id}/content` | The raw file bytes |
| `POST /v1/batches` | Create a batch from an uploaded input file — input validated up front; executed in the background by the worker |
| `GET /v1/batches` | List the calling key's batches (cursor-paginated) |
| `GET /v1/batches/{id}` | Batch status + `request_counts` + output/error file ids once completed |
| `POST /v1/batches/{id}/cancel` | Cancel an in-progress batch |
| `POST /v1/feedback` | Rate a request the calling key made (by its `X-Request-Id`; rating 1–5, optional comment) — see [Feedback](#feedback) |

Diagnostic response headers on every data-plane response (completions, embeddings, images, audio): `X-Request-Id`, `X-Gateway-Model`, `X-Gateway-Provider`, `X-Gateway-Deployment`, `X-Gateway-Cache` (HIT/SEMANTIC/MISS/IDEMPOTENT; NONE on the non-cacheable surfaces), `X-Gateway-Cost-Usd` (the *billed* cost — the tenant's marked-up price when `pricing_markup_pct` is set), `X-Gateway-Latency-Ms`.

### Native client formats (Anthropic & Gemini)

Point an Anthropic or Gemini SDK at the gateway and get the same policy pipeline (keys, guardrails, budgets, routing, caching) with responses in the client's own format:

| Endpoint | Description |
|---|---|
| `POST /v1/messages` | Anthropic Messages API — request/response/streaming translation (tools, images, system, tool results) |
| `POST /v1/models/{model}:generateContent` | Gemini generateContent (also on `/v1beta/...`) |
| `POST /v1/models/{model}:streamGenerateContent` | Gemini streaming (SSE; also on `/v1beta/...`) |
| `POST /v1/models/{model}:embedContent` | Gemini single embedding — `{embedding: {values}}` (also on `/v1beta/...`) |
| `POST /v1/models/{model}:batchEmbedContents` | Gemini batch embeddings — `{embeddings: [{values}]}` (also on `/v1beta/...`) |

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://localhost:8000", api_key="sk-gw-...")
resp = client.messages.create(
    model="gpt-4o",  # any public model name from config.yaml
    max_tokens=256,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

Auth on every endpoint accepts the same virtual/admin keys via `Authorization: Bearer`, `x-api-key` (Anthropic SDKs), `x-goog-api-key` / `?key=` (Gemini SDKs) or `api-key` (Azure OpenAI SDKs). Errors come back in each format's native envelope (Anthropic `{"type": "error", ...}`, Gemini `{"error": {"code", "status"}}`), and the diagnostic headers above are preserved on native responses.

## Agent frameworks

Anything that lets you override `base_url` + `api_key` works against the gateway: OpenAI SDK, LangChain, LlamaIndex, CrewAI, AutoGen/AG2, PydanticAI (all speak chat completions), the Anthropic SDK and google-genai SDK (native inbound, above). Point them at `http://localhost:8000/v1` with a gateway key.

**OpenAI Agents SDK** speaks the Responses API by default (`POST /v1/responses`), which the gateway bridges to its chat-completions core — verified against the real SDK, streamed and non-streamed, including the tool-call roundtrip:

```python
from agents import Agent, Runner

agent = Agent(name="assistant", instructions="...", model="gpt-4o")  # public name from config.yaml
result = await Runner.run(agent, "Hello!")  # hits POST /v1/responses on the gateway
```

The bridge covers the chat-convertible subset (input items, `instructions`, function tools, `tool_choice`, `text.format`, `parallel_tool_calls`, streaming SSE events). If you hit a gap, `use_responses=False` (or `OpenAIChatCompletionsModel`) routes the same agent through `POST /v1/chat/completions` instead.

Things to know when pointing a framework at the gateway:

- **Model names are the gateway's, not the provider's** — clients must send a public name from `config.yaml` (unknown models → `404 model_not_found`; `GET /v1/models` and `GET /v1/models/{model}` answer in the OpenAI shape so framework startup checks pass). Apps you can't re-code can keep their hardcoded provider names via `model_aliases` (see [Configuration reference](#configuration-reference-configyaml)).
- **Run guardrails in observe mode first** on agent workloads — agents quote instructions and process user-supplied text, which trips heuristics like `prompt_injection` more often than chat UIs do; PII redaction also *changes* the text the model sees. `mode: observe` in `config.yaml` measures verdicts without blocking.
- **Bursts**: agent tool loops fire many near-simultaneous requests. RPM limits count cache hits too (they protect the upstream either way), and 429s carry `Retry-After`, which OpenAI-SDK-based frameworks honor automatically — but size `rpm_limit` for the loop, not the user.
- **Caching**: identical requests share exact-cache entries *across keys* (agents sending the same system prompt + task benefit a lot). The cache key covers model, messages, sampling params, tools, `response_format`, `logprobs`/`top_logprobs`/`logit_bias` — but not `user`/`metadata`. The semantic cache (opt-in) serves *similar* answers; think twice before enabling it for agents that parse the text to make tool decisions.
- **Streaming errors** surface as an error event followed by stream end, not an HTTP error — same as OpenAI.
- **Browser-based agents**: enable CORS with `GATEWAY_CORS_ALLOW_ORIGINS="https://your-app.example"` (comma-separated; off by default) and send the key via any accepted header.
- **Tracing bonus**: frameworks that propagate W3C `traceparent` join the gateway's traces end to end, up to and including the provider call.

### Admin API (requires an admin key — platform admin for everything, tenant admin for the tenant-scoped rows)

| Endpoint | Description |
|---|---|
| `GET /admin/ui` | The admin dashboard (static HTML shell, no auth — it contains no data; the key is entered in the browser) |
| `POST /admin/keys` | Create a virtual key (returns the raw key **once**); `is_admin` grants admin rights (a platform delegate outside tenants, a tenant admin inside one), `tenant_id` places it (platform admins only; a tenant admin's creates are forced into their own tenant — a mismatching value is a 400) |
| `GET /admin/keys` / `GET /admin/keys/{id}` | List/inspect keys (tenant admins see their own tenant's only; cross-tenant ids 404) |
| `PATCH /admin/keys/{id}` | Update limits, allowlists (`allowed_models` / `allowed_tools` / `allowed_mcp_servers`), active flag, budgets (total / daily / monthly), `disabled_guardrails`, `model_mappings` / `default_model`, prompt policy (`system_prompt` / `system_prompt_ref` / `prompt_variables`), scoping (`allowed_endpoints` / `allowed_ips`; explicit `null` clears — changing `max_budget_usd` re-arms budget alerts), and `grace_until` (rotation's incident knob: explicit `null` kills the old credential at once, a future timestamp re-arms its window); `tenant_id` re-homes the key (**platform admin only**, target validated — tenant admins get 403) |
| `DELETE /admin/keys/{id}` | Revoke a key |
| `POST /admin/keys/{id}/rotate` | **Rotate a key's credential** (tier 51): the row keeps its identity — name, tenant, budgets, spend, limits, logs all stay — while a new raw key is issued and shown exactly once, like creation. Body: `grace_seconds` (default 0 = immediate cutover); until the window ends, **both** credentials authenticate as the same key, then the old one fails with an explicit "rotated" 401. Rotating again supersedes the previous credential mid-window; revocation and expiry still refuse both. Tenant admins rotate their own tenant's keys (cross-tenant ids 404). Audited as `key.rotated` |
| `POST /admin/tenants` | Create a tenant (platform admin): slug `name` (header-safe pattern — it surfaces in `X-Gateway-Tenant` and log filters), `display_name`, model allowlist, tool allowlist (`allowed_tools`), RPM/TPM, `pricing_markup_pct` (cost-plus billing), budgets |
| `GET /admin/tenants` / `GET /admin/tenants/{id}` | List/inspect tenants, with pooled spend and fired alert levels (platform admin) |
| `PATCH /admin/tenants/{id}` | Update a tenant's policy ceiling (slug immutable) — `is_active: false` disables every member key at once; budget changes re-arm tenant alerts; `pricing_markup_pct` applies from the next request (auth-time snapshot) |
| `DELETE /admin/tenants/{id}` | Delete a tenant — refused with 409 while any key still references it (a dangling `tenant_id` fails closed at auth time, so re-home or revoke first) |
| `GET /admin/vault` | The BYOK vault at a glance (admins): `enabled` or the honest `unavailable_reason` (names the env var, never a value), the `strict` stance (live per call — hot reloads show on the next poll), `invalidation_backend`, `ttl_seconds`, and `admin_context` (the tenant a *tenant admin* manages — their own; platform admins get null and pick from the list). No secrets anywhere in the payload |
| `GET /admin/tenants/{id}/provider-keys` | List a tenant's stored provider credentials (BYOK vault) — metadata only: provider, label, `key_hint` (last characters, never the key), timestamps. Tenant admins see their own tenant's only; cross-tenant ids 404 |
| `PUT /admin/tenants/{id}/provider-keys/{provider}` | Store (or replace) a tenant's credential for one provider — from the next request on, that tenant's traffic through that provider rides their account. Requires the vault (`GATEWAY_VAULT_MASTER_KEY`, else 400 `vault_unavailable`); the response carries only the hint; audited with the hint, never the key |
| `DELETE /admin/tenants/{id}/provider-keys/{provider}` | Remove a stored credential — the next request rides the platform's configured key again (404 when nothing is stored) |
| `POST /admin/prompts` | Append a version to a named prompt (first version of a name auto-activates) |
| `GET /admin/prompts` | One summary per prompt name: active version, A/B candidate, version count |
| `GET /admin/prompts/{name}` | All versions of one name, newest first |
| `PATCH /admin/prompts/{id}` | Activate a version (promote or rollback — switches serving, no restart), mark it as the A/B **candidate** (unique per name; `ab_fraction` clamped to 0–1), or edit its notes |
| `DELETE /admin/prompts/{id}` | Delete one version (the active version is refused with 409 — activate another first) |
| `GET /admin/logs` | Paginated request logs with filters (bodies omitted for size), rows carry `tenant_id` (`?tenant_id=` filter; forced to the admin's own tenant for tenant admins); shadow-mirror rows are included and flagged `shadow`, prompt A/B mirrors also carry `prompt_variant`, and scored mirrors carry `shadow_verdict` + `shadow_similarity` (the tier-35 pair score) |
| `GET /admin/feedback` | Feedback rows, newest first — `key_name`/`model`/`request_id`/`min_rating`/`max_rating` filters; the same forced tenant scoping as logs |
| `GET /admin/feedback/stats` | Quality aggregates from client feedback: overall + by-model + by-key buckets (`count`, `avg_rating`, `pct_negative` [1–2], `pct_positive` [4–5]); `model`/`key_name`/`tenant_id` filters |
| `GET /admin/canaries` | Last synthetic-probe result per deployment (`outcome` ok/timeout/error, latency, last checked; `unknown` before the first wave) + the canary config echo — platform admin only (deployment health is platform configuration) |
| `POST /admin/canaries/probe` | Run one probe wave now, regardless of `canary.enabled` — "probe now" for ops; returns fresh per-deployment results |
| `GET /admin/pii-scan` | Scan recent request-log bodies for PII/secrets (bulk, `limit`/`key_name`/`model`/`status`/`tenant_id` filters, bounded by `pii_scan.max_rows`): per-row findings for flagged rows + `by_kind` aggregates; `bodies_not_captured` counted separately from clean; pure read, nothing persisted |
| `GET /admin/logs/{id}/pii-scan` | PII/secret findings for one log row (kind + count only — the matched text is never returned) |
| `GET /admin/logs/{id}` | One log row in full, including the stored request/response bodies (cross-tenant ids 404 for tenant admins) |
| `POST /admin/playground/chat` | The admin playground (tier 41, platform admin): one chat completion through the **real pipeline** from the dashboard — body `model` + `messages` (+ optional `key`, `max_tokens`, `temperature`; `stream: true` is a 400). `key` names a virtual key to impersonate, resolved through the same chain the JWT auth step uses, so its allowlists, budgets, residency, guardrail scopes and rate limits bind and its spend accrues (omitted = the calling admin's own, unrestricted context); every row and event carries `auth_method: playground`. Returns the client-facing completion verbatim beside the request-log row the run just wrote as `diagnostics` (deployment, provider, status, tokens, cost, latency, cache-hit, guardrail verdicts). Errors pass through as the envelope a real client would receive; every attempt is audited as `playground.chat`. See [Admin playground](#admin-playground) |
| `GET /admin/slos` | Service level objectives (tiers 42–44, platform admin): every `slos:` target measured over its rolling window and verdicted — `met` / `breached` (with the reason saying by how much) / `insufficient_evidence` (fewer than `min_requests` requests, or nothing streamed for a TTFT target — a quiet window is not a verdict). Each row echoes the definition beside the full measurement (requests/errors/error-rate, latency p50/p95/p99, TTFT p95), the target metric's value, the check, the error-budget lens (tier 43): `budget` (the bar's bad-event allowance, the bad count, burn rate = observed/allowed, remaining, and the `on_track`/`at_risk`/`exhausted`/`insufficient_evidence` state) and `burn` (each `slos.burn` rule's multiwindow pace verdict — the long window establishes the burn, the short one confirms it is *still* burning, so a burst that ended never fires), plus the target's persisted `alert` state (tier 44 — firing/condition/since, what on-call was already paged for; `null` when never fired or recovered). Client experience, not deployment health: non-shadow, non-trial rows, cache replays included, unattributed errors included at model scope; a deployment-scoped target measures only its attributed rows. Hot-reloadable — a tightened bar re-verdicts on the next read. The numbers always ship beside the verdict. See [Service level objectives (SLOs)](#service-level-objectives-slos) |
| `GET /admin/stats` | Requests, error rate, tokens, cost, latency + TTFT percentiles (client traffic only — shadow mirrors are excluded); tenant admins see their own tenant's traffic, platform admins additionally get a `by_tenant` breakdown |
| `GET /admin/usage/export` | Billing / chargeback export: `format=csv\|json`, `since`/`until` (ISO-8601, default last 30 days), `key_name`/`model`/`status`/`tenant_id` filters, `include_shadow`; JSON adds totals + per-key/model/status aggregates (+ per-tenant for platform admins); tenant admins export their own tenant only |
| `GET /admin/invoices` | Usage invoices (tier 45): a month statement of what each key and tenant consumed — `month=YYYY-MM` (calendar months in UTC, default the current month, future = 400), the request-log rows rolled up into per-(key, model) line items with requests/errors/token splits/cost, the key's and tenant's `monthly_budget_usd` beside the spend (`budget_pct`), zero-usage keys included, deleted keys billed under their tenant with no budget; billing lens = client-consumed usage (shadow mirrors and trial replays excluded, cache hits included at the cached rate); `format=csv` = one row per line item in a stable column order, attachment `invoice_<month>.csv`; tenant admins forced to their own tenant, platform admins may `?tenant_id=` filter. See [Billing & usage invoices](#billing-usage-invoices) |
| `GET /admin/anomalies` | Cost anomaly flags (tier 46): unusual money and traffic patterns — the recent window (`finops.window_hours`, default 24h) vs each key's trailing baseline (`finops.baseline_days`, default the 14d before it), per key and per model over the client-consumed lens. Four kinds: `cost_spike` / `traffic_spike` (≥ `finops.spike_ratio` the baseline's daily rate), `traffic_stop` (real baseline traffic gone silent), `first_seen_model` (a model with zero baseline traffic appeared); thin baseline (< `finops.min_baseline_requests` rows) = no verdict, brand-new keys cannot spike, zero-cost baseline with window spend = `ratio: null` (never an invented ∞); every flag carries window/baseline numbers, a ratio where one exists and a human message, the bounds in the envelope; informational only — no loop, no state, no delivery; `finops.enabled: false` = an honest empty list while the invoice keeps serving; tenant admins forced to their own tenant, platform admins may `?tenant_id=` filter. See [Finops anomaly flags](#finops-anomaly-flags) |
| `GET /admin/forecasts` | Spend forecasts (tier 47): the month's final bill projected — run-rate (spend so far over the elapsed fraction, scaled to the month) and recent-pace (the last 7 days' daily spend carried over the remaining days) per key and per tenant over the billing lens, with the trend ratio (recent daily over month-to-date daily) beside them and projected-vs-budget verdicts (`over_budget` / `at_risk` ≥80% / `within_budget`, `verdict_based_on` says which projection decided — the recent pace when both exist); a past month serves its actual total as the projection (`month_complete: true`, remaining 0, trend null), a future or malformed month is the invoice's 400; a trend needs 2+ days of month and a nonzero average (null otherwise, never a guess or an invented ∞); every existing key included (an unused key projects $0 of its budget), deleted keys project where they lived; the clock context (`elapsed_days` / `remaining_days` / `days_in_month`) ships in the envelope; tier 48 attaches each section's persisted budget-alert state (`alert`: firing, condition, since — what on-call was already paged for, from the same settings row the evaluator wrote, `null` when never fired or recovered) and echoes the `forecast_alerts:` delivery config; tenant admins forced to their own tenant, platform admins may `?tenant_id=` filter. See [Spend forecasts](#spend-forecasts) |
| `GET /admin/routing` | The routing table (public models → deployments, with each deployment's circuit-breaker state; shadow deployments flagged with their mirror fraction; each deployment's `residency` claim visible — `null` means "no claim, satisfies no residency requirement") |
| `GET /admin/shadow-diff` | Migration evidence over mirrored traffic: one group per (model, shadow deployment) with pair counts, the verdict breakdown (`exact`/`near`/`divergent`/`shadow_error`/`not_comparable`), match rate, mean similarity, and latency/token/cost deltas against the paired primaries; `?model=`, `?since=` scope it. Platform-admin-only — the pairs carry request bodies, which cross tenants. Deployment mirrors only (prompt A/B rows are excluded by design) |
| `GET /admin/shadow-diff/pairs` | The drilldown behind the aggregate: scored pairs for one `?model=` + `?deployment=`, newest first, optional `?verdict=` filter (`divergent` = the migration review queue), `limit`/`offset` paging. Each pair carries both sides' status/latency/tokens/cost and text excerpts of the stored bodies (null when `logging.store_bodies` was off — the verdict itself always exists) |
| `POST /admin/trials` | Start a candidate trial (tier 36, platform admin): deliberately replay a captured traffic window of one `model` (its successful, body-storing chat rows in `since`/`until`, newest first, up to `limit`) against a `deployment` candidate — any configured deployment, it need not belong to the replayed model. Real upstream calls in waves of `concurrency`, cumulative candidate spend capped at `max_cost_usd` (all three clamped to the `trials:` ceilings); each entry writes a scored shadow row sharing the original's request id. See [Candidate trials](#candidate-trials) |
| `GET /admin/trials` | Trial runs, newest first: status, progress (`selected`/`completed`/`skipped`), cumulative candidate spend, stop reason (`done`/`no_entries`/`cost_cap`/`cancelled`/`orphaned_by_restart`/`error`), the per-run verdict summary **and its go/no-go recommendation** folded into every row |
| `GET /admin/trials/{id}` | One run: the row plus its verdict summary (exact/near/divergent/shadow_error/not_comparable counts, match rate, mean similarity, latency means vs the captured primaries) and its recommendation |
| `GET /admin/trials/{id}/pairs` | The trial drilldown — the same pair shape as `/admin/shadow-diff/pairs` (both sides' status/latency/tokens/cost + text excerpts), scoped to the run; `?verdict=divergent` is the trial's review queue. Trial rows are excluded from the standing shadow-diff aggregate (an experiment, not live mirroring) |
| `POST /admin/trials/{id}/cancel` | Cancel a pending/running trial. Guarded on the current status (finished runs answer 409); a live one stops within one wave — the runner re-checks the row between waves, so the DB status is the truth on every replica |
| `GET /admin/migration/verdicts` | Go/no-go recommendations (tier 37, platform admin) over the *mirrored* migration evidence: one row per scored shadow-deployment group — the full evidence group plus its `recommendation` (`verdict` go/hold/no_go/insufficient_evidence, per-check `checks`, failed-check `reasons`). `?model=`/`?deployment=` narrow, `?since=` (ISO-8601) bounds the window; the live `migration:` thresholds are echoed. Trial runs carry their own recommendations on `/admin/trials`. See [Migration verdicts](#migration-verdicts) |
| `POST /admin/migration/plan` | Render the exact config.yaml edits a promotion would apply (tier 38, platform admin) — and the evidence verdict justifying them — with **no side effects**. Body: `model` (the public model to promote into), `deployment` (the candidate), `weight` (1–100, default 10 — the canary weight inside the incumbent's priority tier). Returns the `mode` (`shadow_flip` / `rebalance` / `cross_model_insert`), structured `changes`, a unified `diff` of the file, the resulting `canary_share`, `warnings`, and the `recommendation` with its `evidence_source` (mirror / `trial:<id>` / none). See [Migration promotion](#migration-promotion) |
| `POST /admin/migration/promote` | Apply it (tier 38, platform admin): backup → edit config.yaml → the same hot-reload path `POST /admin/reload` takes → audit `promotion.applied` — and if the reload refuses, the backup is restored and the running config never moved. A `no_go` verdict is a 409 `promotion_blocked` unless `force: true` (audited as forced); `hold`/`insufficient_evidence` promote with the concern attached as a warning. `promote` re-builds the plan from the file at apply time — a preview is never applied stale. See [Migration promotion](#migration-promotion) |
| `GET /admin/migration/backups` | The promotion backups every apply writes next to config.yaml (`config.yaml.bak-<utc stamp>`), newest first (tier 39, platform admin). Pure read. Each row explains the backup: `created_at` (the stamp), `size_bytes`, whether it still `parses` (an old backup can predate schema changes — honestly labeled, refused at apply time), `is_current` (byte-identical to the live file), and the structured `changes` restoring it would apply (`shadow`/`unshadow` flags re-set, `set_weight`/`set_priority` edits, `add_deployment`/`remove_deployment`). See [Promotion rollback](#promotion-rollback) |
| `GET /admin/migration/backups/{name}` | One backup in full (tier 39, platform admin): the listing row plus the line-level `diff` of the live file → the backup's text — the human preview of what rolling back applies. Flat names only (path separators never pass validation). See [Promotion rollback](#promotion-rollback) |
| `POST /admin/migration/rollback` | Restore config.yaml from a promotion backup — the deliberate undo of a promotion (or a ladder of them), the same apply discipline in reverse (tier 39, platform admin). Body: `backup` (flat `config.yaml.bak-<stamp>` name; omitted = the latest, "undo the last promotion"). The chosen backup must parse; a byte-identical one is refused as the no-op it is (409 `rollback_noop`); the live file is backed up **first** (the rollback's own undo point), then restored, hot-reloaded (source `rollback`) and audited as `promotion.rolled_back`. On reload refusal the undo point is restored and the running config never moved. See [Promotion rollback](#promotion-rollback) |
| `GET /admin/migration/watch` | Post-promotion watch (tier 40, platform admin): the migration story continued after the promotion flips the candidate live and the mirrors stop. One row per serving deployment, re-verdicted on the rows attributed to it (mirrors, trial replays and cache replays excluded) from when it started serving the model — its requests/errors/error-rate/latency/cost means, the tier peers' aggregate over the same window, the expected→actual share split, and a go/hold/no_go/insufficient_evidence recommendation from the same `migration:` thresholds. A recommendation to keep serving or roll back — never an automatic one. `?model=`/`?deployment=` narrow, `?since=` overrides the window. See [Post-promotion watch](#post-promotion-watch) |
| `GET /admin/mcp` | The MCP proxy at a glance: enabled flag, session TTL, each configured server's url/timeout, the **names** of its auth headers (never the values) and how many gateway sessions are open against it (platform admin) |
| `POST /admin/routing/breakers/{deployment}/reset` | Force-close a deployment's circuit (next request probes it again) |
| `GET /admin/runaway` | Runaway-agent protection view: keys currently in the loop-detection cooldown jail (remaining cooldown, offense count, key name), the live `runaway:` config echo and the `enabled` flag. In-process state — a multi-replica fleet reports its own jail here (platform admin) |
| `POST /admin/runaway/{key_id}/release` | Release a key from the runaway jail (escape hatch): clears the jail, the offense history and the loop windows — the key serves again immediately, and a still-looping agent re-trips from a *first-offense* cooldown. Audited as `runaway.released` |
| `GET /admin/concurrency` | Concurrency admission control view: keys/tenants currently holding in-flight slots or queueing for one (with key/tenant name joins), the live `concurrency:` config echo and the queue total. Read-only — slots free themselves as responses complete; the caps live on the key/tenant rows. In-process state, so a multi-replica fleet reports its own share (platform admin) |
| `GET /admin/guardrails` | Registered guardrails, their params and mode (enforce/observe) |
| `POST /admin/reload` | **Hot-reload config.yaml** without a restart (same as SIGHUP): guardrails, models/deployments, routing policy, pricing and prompt knobs swap in place; an invalid config is a 400 with the running one untouched; fields that cannot be hot-swapped (provider credentials, auth chain, cache backends, telemetry) are listed in `restart_required` |
| `POST /admin/backup` | Snapshot the SQLite database (online `VACUUM INTO` — consistent copy of the live DB, no downtime) into `GATEWAY_BACKUP_DIR`; returns name + size. SQLite-only: with a PostgreSQL `GATEWAY_DATABASE_URL` the call is refused with a 400 (`backup_not_supported`) — use `pg_dump` |
| `GET /admin/backups` / `DELETE /admin/backups/{name}` | List / delete backups (flat names only). Restore is an offline procedure: stop the gateway, replace the DB file, start again |
| `GET /health`, `GET /healthz`, `GET /readyz`, `GET /metrics` | Health (compat), liveness, readiness (503 while starting / draining / DB-down), Prometheus metrics |

Example — issue a scoped key with per-team model steering:

```bash
curl -X POST http://localhost:8000/admin/keys \
  -H "Authorization: Bearer $GATEWAY_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "mobile-app",
        "allowed_models": ["gpt-4o", "gpt-4o-mini"],
        "rpm_limit": 60,
        "tpm_limit": 100000,
        "max_budget_usd": 25.0,
        "daily_budget_usd": 5.0,
        "monthly_budget_usd": 100.0,
        "model_mappings": {"gpt-4o": "gpt-4o-mini"},
        "default_model": "gpt-4o-mini",
        "system_prompt_ref": "support-persona",
        "prompt_variables": {"company": "Acme"},
        "allowed_endpoints": ["chat", "models"],
        "allowed_ips": ["10.0.0.0/8", "192.168.1.15"],
        "allowed_tools": ["search_*", "get_weather"],
        "allowed_mcp_servers": ["weather-*"]
      }'
# → { "key": "sk-gw-...", ... }   (shown only once)
```

`system_prompt_ref` names a prompt in the library (see the Prompts tab / `POST /admin/prompts`); its active version serves as the key's system prompt — `{{company}}` inside that prompt body resolves with the key's `prompt_variables`. Editing the prompt later is a new version + activate: no key change, no restart. An inline `system_prompt` on the same key is the fallback the ref overrides.

## Key rotation & grace windows

Credentials age — they leak into a repo, they sit in too many CI variables, they just get old. `POST /admin/keys/{id}/rotate` replaces the *secret* while the row keeps its identity: name, tenant, allowlists, budgets, limits, spend and logs all stay put, so callers switch credentials without switching identity. The new raw key is returned exactly once, like creation; only its hash is stored.

```bash
curl -X POST http://localhost:8000/admin/keys/<id>/rotate \
  -H "Authorization: Bearer $GATEWAY_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"grace_seconds": 3600}'
# → { "key": "sk-gw-...", "grace_until": "...", "previous_key_prefix": "sk-gw-..." }
```

The rules, in the order they bite:

- **Grace window.** For `grace_seconds` after rotation, *both* credentials authenticate as the same key — budgets, limits, spend and logs accrue to the row whichever was presented. After the window the old credential fails with an explicit 401 ("This API key has been rotated — its grace window has ended") instead of a generic unknown-key error. `grace_seconds: 0` (the default) is an immediate cutover — the old hash is still remembered, so its holders get the honest message.
- **The row's own checks fire first.** Revocation (`DELETE /admin/keys/{id}`) and `expires_at` refuse *both* credentials, mid-window or not — rotation never resurrects a revoked key or moves an expiry deadline.
- **One previous credential.** Rotating again supersedes the old one outright: the credential from two rotations ago stops working the instant the second rotation lands, even mid-window.
- **The incident knob.** `PATCH /admin/keys/{id}` with `"grace_until": null` kills the old credential immediately (the old key leaked mid-rotation); a future timestamp re-arms or extends its window.
- **Audited, never leaked.** Each rotation emits a `key.rotated` audit record (who, which key, the grace window) — and no record, log or public API surface ever carries either raw key or the previous key's hash. JWTs keep working across rotation untouched: a token names the *key*, not the credential.

The admin UI puts a Rotate button on every active key (grace prompt → the one-time raw-key reveal), and rows in grace show an amber badge with the dying prefix and the window's end. 12 tests pin the contract (`tests/test_key_rotation.py`).

## Key expiry warnings

`expires_at` has always been enforced — a key past its deadline answers 401 "This API key has expired." — but until tier 52 nothing *announced* the deadline. The operator's first signal was the user complaint. The budget-alert machinery, deadline edition, closes that: a background sweep (hourly by default, `alerts.expiry_interval_seconds`) walks every active key with an `expires_at` and fires once per day-level as the deadline approaches — `alerts.expiry_days` (default **14/7/1/0**; 0 is the page that the key just died, the one the 401 already gives callers but on-call never saw coming).

```yaml
alerts:
  expiry_days: [14, 7, 1, 0]        # day levels; 0 = the expired page
  webhook_url: https://hooks.slack.com/services/T000/B000/XXXX
```

The rules, in the order they bite:

- **Fire-once per level, highest first.** Each level fires once per key and persists on the row (`expiry_alerts_fired`, beside the budget levels) *before* delivery — a restart never re-pages, and a crash mid-sweep prefers duplicate silence over duplicate noise. When a pass crosses several levels at once (the key was discovered already past its deadline), each fires its own page, highest first — never one merged page.
- **Setting or moving a deadline arms silently.** A key created 6 days from its deadline does not page "expires in 14 days" seconds after the operator chose that deadline: every level already crossed is marked fired, the same way raising a budget re-arms below-current thresholds. Only *future* crossings alert — moving a deadline **out** honestly re-arms the levels it now sits above; moving it **in** never pages.
- **Discovery is honest.** A key the sweep finds with a near deadline and no fired state (it predates the feature) fires its already-crossed levels — "you have a key expiring in 3 days" is exactly the page this exists for.
- **Revoked and deadline-less keys never warn.** A dead key being dead is not news, and a key with no deadline has nothing to warn about. An expiring key with zero traffic still pages — spend-triggered alerts structurally cannot see a quiet key.
- **Rotation never touches the state** (tier 51 interaction): a deadline belongs to the row, whichever credential presents it.
- **Delivery.** Best-effort on the same webhook as budget alerts (`type: expiry_alert`, with key, level, days remaining, deadline and a human message), one retry, counted on `gateway_alerts_total{kind="expiry",outcome=...}`; without a webhook URL the POST is a no-op counted `skipped` while the `expiry.threshold` observer events still fire. `alerts.enabled: false` stops the sweep entirely.

The admin UI shows an amber **⏳ expiring** badge on active keys inside the first warning level (14 days), and the sweep's clock never blocks requests. 10 tests pin the contract (`tests/test_expiry_alerts.py`).

## Realtime API

`WS /v1/realtime?model=<public-model>` is the fifth inbound surface and the first **stateful** one: HTTP surfaces are one request, one response, one policy pass — a realtime session is a long-lived bidirectional event stream, and the gateway holds the matching upstream WebSocket open for the session's whole life. Speak the OpenAI Realtime event protocol on both legs:

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect(
        "ws://localhost:8000/v1/realtime?model=gpt-4o-realtime",
        additional_headers={"Authorization": "Bearer sk-gw-..."},
    ) as ws:
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] == "session.created":
                await ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": "Answer briefly.",
                        "input": [{"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": "Hello!"}]}],
                    },
                }))
            elif event["type"] == "response.output_text.delta":
                print(event["delta"], end="", flush=True)
            elif event["type"] == "response.done":
                return

asyncio.run(main())
```

Auth accepts the same keys as every surface (`Authorization: Bearer`, `x-api-key`, `x-goog-api-key`, `api-key`) — plus `?key=` in the query string, because browsers cannot set WebSocket headers. A deployment is realtime-capable when its provider carries a `realtime_base` (e.g. `wss://api.openai.com/v1`); the session's upstream handshake authenticates in the provider's own header shape (Azure `api-key`, Anthropic `x-api-key` + version, Gemini `x-goog-api-key`), BYOK vault included — a tenant that keyed the provider connects with *their* credential.

**Session semantics** — the pipeline applies policy where a session actually differs from a request:

- **Open gates run once, accept-first**: model permission, the `realtime` endpoint scope, RPM (one hit at open; tokens accrue per response), the budget check, and a concurrency slot **held for the session's life** (`max_concurrency` counts sessions — they occupy upstream capacity like an in-flight stream). Every failure to open is delivered in-band — an OpenAI-shaped `{"type": "error", "error": {...}}` event, then close 1008 — so a client with no HTTP surface still gets a debuggable message instead of a bare handshake refusal. The budget is also **re-checked on every `response.create`**: exhausting it mid-session ends the session the same way, with the row recording why.
- **Routing at connect time only**: the residency- and breaker-filtered ladder is walked while opening the upstream handshake (failing over between legs), then the session *stays put* — a long-lived session cannot be retried mid-stream. A provider without `realtime_base` is never realtime-routed; a model with no realtime-capable deployment answers with `realtime_unavailable` instead of a silent HTTP fallback.
- **Guardrails, per event**: the request phase runs over each `response.create` — over its own `input` (fork semantics), or over the **shadowed conversation** the relay keeps when the create carries none — and a BLOCK kills that response, not the session: an error event, nothing forwarded upstream. The stream phase and held-back rewriting run over `response.output_text.delta` (rewriting guardrails see the text the client receives, with the final flush at `output_text.done`, so nothing a rewriter masks leaks no matter how the provider chunked; block checks see the source text). A mid-response stream BLOCK sends the error event, forwards `response.cancel` upstream and suppresses the rest of *that* response — the session lives on, and the cancelled response's `response.done` still bills (the provider ran). Text is the guarded surface; audio deltas pass through.
- **Accounting**: **one** request-log row per session (`route: realtime`, `stream: true`) with tokens, cost and spend summed across responses — input tokens map to prompt, TTFT measured on the first delta — so budgets, exports and `/admin/logs` see realtime traffic exactly like HTTP. Deliberately not on this surface: the response caches, idempotency keys, runaway-agent detection and `X-Gateway-*` diagnostic headers (a WebSocket has no response headers; the log row and metrics carry the same information).

`gateway_realtime_sessions{provider,model}` gauges open sessions; close the client socket and the gateway closes the upstream one, releases the slot and writes the row on the way out.

## MCP proxy

`POST /v1/mcp/{server}` is the sixth inbound surface, and the first whose payload is not for the gateway's models at all: a transparent **Model Context Protocol** proxy (Streamable HTTP transport, spec 2025-06-18) between your MCP clients and operator-configured MCP servers — the gateway sits in the middle with its keys, its policy and its logs, and the servers never know.

```yaml
mcp:
  enabled: true
  session_ttl_seconds: 3600
  servers:
    - name: weather
      url: https://mcp.internal.example/mcp
      headers:                      # literal dev/test values
        Authorization: "Bearer upstream-secret"
      headers_env:                  # production: env var names, resolved at call time
        Authorization: MCP_WEATHER_TOKEN
      timeout_seconds: 30           # optional per-server call timeout
```

- **One message per POST.** The 2025-06-18 transport removed batching: an array body answers `-32600`, a non-JSON body `-32700` (both transport 400s in the JSON-RPC envelope, so MCP client SDKs can parse the rejection). `initialize` creates a session; notifications (and client answers to server-initiated requests) relay upstream and answer **202**; everything else requires the `Mcp-Session-Id` header (missing → 400 `-32000`, unknown/expired → 404 `-32003`).
- **The gateway issues its OWN session ids** (`gw_…`) and maps them to the upstream session plus the negotiated protocol version, stamping both (`Mcp-Session-Id` / `MCP-Protocol-Version`) on every post-initialize upstream call. A client session id is useless upstream; a gateway restart or id rotation breaks nothing there. `DELETE /v1/mcp/{server}` terminates (local drop first — idempotent for the client — then the upstream DELETE relayed, 405 allowed); an upstream 404 drops the mapping lazily so the next message fails at the gateway and the client re-initializes, exactly as the transport spec expects. Idle sessions are swept after `session_ttl_seconds`.
- **The caller's credential never travels upstream.** Upstream auth is the operator's concern — the configured `headers` / `headers_env` and nothing else; the client's gateway key stays in the gateway. BYOK does not apply to MCP servers (there is no per-tenant notion of "your" weather server); `GET /admin/mcp` shows each server's auth-header **names** only.
- **Tool policy, per name** (the tier-30 machinery): `tools/list` is filtered to the names this key could actually *call* (a restricted key never learns the catalog it is denied), and `tools/call` is admitted one name at a time — a denial is an **in-band JSON-RPC error** (`-32001`, HTTP 200) so the agent's JSON-RPC layer sees which tool and why; nothing upstream ever sees a denied call. A nameless call under a tool policy fails closed. Admin keys bypass the key layer; tenants can only narrow (`allowed_mcp_servers` follows the same two-layer model-permission shape: key ∩ tenant, fnmatch patterns; unknown/not-allowed servers answer 404 exactly like missing ones).
- **Guardrails over every content-bearing method (tier 49).** On `tools/call`, the request phase runs over the serialized arguments (a synthetic chat request whose user message is the sorted-JSON args) and the response phase over the result: each `text` item, the text of embedded-resource items, and `structuredContent` (guarded as serialized JSON). The same both-direction pass covers **`resources/read`** (the whole `params` — uri included — in; `contents[].text` out), **`prompts/get`** (`params` incl. `arguments` in; message text blocks out, in both content shapes — a single block or a list, because the spec widened mid-life and servers ship both) and **`completion/complete`** (`params` incl. the completion `argument` in; `completion.values[]` out). A BLOCK is the same in-band error (`-32002`); a MODIFY is applied **only if the rewritten text is still a valid JSON object** — the arguments rule, enforced on arguments, params and `structuredContent` alike — otherwise the call fails closed rather than corrupting it; a model-redirect verdict has no meaning on this surface and fails closed too. Base64 payloads — image/audio items, resource `blob`s — are binary: no text to scan, skipped by design and passed through byte-identical. Everything else stays verbatim by design: lifecycle (`initialize`), notifications, catalog reads (`tools/list`, `resources/list`, `prompts/list`), `ping` — nothing the caller wrote beyond a cursor, nothing the server generated beyond static descriptions. Guardrail **scopes bind on this route as on every other**: `routes: ["mcp"]` (the lever for MCP-only policies), `models:` the **server name** (the log row's model column — a guardrail scoped to `weather` governs the weather server and never collides with a chat model), `keys`/`exclude_keys` the calling key's name; the `mcp.tool.blocked` event carries the denied `method` (and the tool name for `tools/call`).
- **Both upstream answer shapes work.** `application/json` is intercepted (filtering/rewriting applied, re-serialized) and `text/event-stream` is relayed **live**: progress notifications and keepalives pass through verbatim — byte-identical, so client read timeouts never fire — and THE response frame is intercepted, re-serialized with policy applied or replaced by an in-band error frame. The standalone GET stream (server→client outside an open POST) is out of scope: GET answers 405, which the spec permits.
- **A documented error split.** *Gateway* failures keep the OpenAI-shaped envelope — auth, the `mcp` endpoint scope, disabled/unknown/not-allowed server, RPM, concurrency (the operator's concerns, identical to every other surface); *protocol-level* failures use the JSON-RPC envelope — the codes above and the in-band denials (the caller's concerns). Upstream HTTP failures relay verbatim: the client's JSON-RPC layer should see the server's own errors.
- **Accounting**: one request-log row per message (`route: mcp`, `provider: mcp`, model = the *server* name — the same role a model name plays everywhere else, which also scopes model-scoped guardrails to servers here), `gateway_mcp_requests_total{server,method,outcome}` (success / error / blocked / relayed) and the `gateway_mcp_sessions{server}` gauge; `mcp.session.opened` / `mcp.tool.blocked` / `mcp.session.closed` events fire for observers. **No tokens, no cost, no TPM, no budgets** — MCP servers are not language models; RPM applies (the realtime precedent), token economics do not, by design. `GET /admin/mcp` introspects the fleet; `mcp:` hot-reloads (a removed server 404s the next message, live sessions stay mapped until they expire — clients re-initialize).

## Batch API

The OpenAI Batch API shape, for bulk chat workloads that don't need to be interactive (evals, backfills, enrichment). Three steps:

```bash
# 1. Upload the input: JSONL, one request per line — custom_id + body
#    (an OpenAI chat completion request), method/url optional but
#    validated when present (only POST /v1/chat/completions runs in
#    batches; "stream" is stripped — batches never stream).
cat > input.jsonl <<'EOF'
{"custom_id": "task-1", "method": "POST", "url": "/v1/chat/completions",
 "body": {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "..."}]}}
{"custom_id": "task-2", "method": "POST", "url": "/v1/chat/completions",
 "body": {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "..."}]}}
EOF
curl -F file=@input.jsonl -F purpose=batch -H "Authorization: Bearer sk-gw-..." \
  http://localhost:8000/v1/files
# → { "id": "file-...", "object": "file", "bytes": 1234, ... }

# 2. Create the batch (input validated synchronously — line rules,
#    body schemas, max_requests — so a created batch is runnable).
curl -H "Authorization: Bearer sk-gw-..." -H "Content-Type: application/json" \
  -d '{"input_file_id": "file-...", "endpoint": "/v1/chat/completions",
       "completion_window": "24h", "metadata": {"job": "nightly-eval"}}' \
  http://localhost:8000/v1/batches
# → { "id": "batch-...", "status": "in_progress",
#     "request_counts": {"total": 2, "completed": 0, "failed": 0}, ... }

# 3. Poll, then fetch results (also GET /v1/batches/{id} from code).
curl -H "Authorization: Bearer sk-gw-..." http://localhost:8000/v1/batches/batch-...
# → status "completed", output_file_id (+ error_file_id if any line failed)
curl -H "Authorization: Bearer sk-gw-..." \
  http://localhost:8000/v1/files/{output_file_id}/content
# → JSONL, one line per request:
#   {"id": "batch-...-L0", "custom_id": "task-1",
#    "response": {"status_code": 200, "request_id": "...", "body": {…completion…}},
#    "error": null}
```

Semantics worth knowing:

- **Full pipeline per line.** Every line executes through `handle_chat` exactly like an interactive request — guardrails (a blocked line fails with the same 400 the client would get, into the error file), model allowlists, routing/retries/failover, response cache, spend accounting, and a request-log row (`request_id` = `<batch id>-L<line>`, `auth_method` = `batch`) so batch traffic shows up in `/admin/logs`, `/admin/stats` and usage exports like any other traffic.
- **Deferral, not failure, for limits.** A rate limit or budget ceiling stops the wave and defers the batch (`retry_not_before`); no line ever fails for those. Budgets are checked per wave against a fresh key snapshot, so spend from earlier lines lands between waves. Raising the key's budget lets a deferred batch finish. The 24h completion window bounds deferrals — a batch that can't finish in time **expires**.
- **Wave-shaped execution.** A background worker runs `wave_size` (16) lines of the oldest runnable batch every `poll_interval_seconds` (2s) — a batch is a steady trickle toward your providers, never a wall of 10k simultaneous requests. The OpenAI SDK's `client.batches` works against these endpoints unchanged.
- **Crash safety.** Results append one row per line (in the same transaction that advances the cursor); a restart resumes from the cursor — at worst the in-flight wave re-runs its lines (duplicate log rows, never lost results). Output files are assembled once all lines have results.
- **Ownership & scoping.** Files and batches belong to the virtual key that created them (another key gets 404s, not 403s) and require the `batch` endpoint scope for scoped keys (`allowed_endpoints`). The platform admin key cannot own files — there is no key row to bill.
- **Config** (`batch:` in config.yaml, hot-reloadable): `enabled` (false = the surfaces 404 and the worker stops), `max_file_bytes` (20 MB), `max_requests` (10k lines), `wave_size`, `poll_interval_seconds`. Progress also flows to `gateway_batch_lines_total{status=completed|failed|deferred}`.

## Lifecycle events

A publish/subscribe layer over the request stages — for side channels (Slack pings, metrics fan-out, custom SIEM feeds, usage meters) without touching gateway code. Configure handlers in `config.yaml`:

```yaml
events:
  enabled: true
  queue_size: 10000
  handlers:
    - class_path: "my_pkg.events:SlackNotifier"  # pkg.module:ClassName
      kind: observer                            # observer | interceptor
      events: ["request.*", "batch.completed"]  # names or patterns; null = all
      params: {webhook_url: "https://hooks.slack.com/..."}
```

```python
from gateway.core.events import EventHandler

class SlackNotifier(EventHandler):
    name = "slack"                            # identifies the handler in
                                              # logs and metrics labels

    def __init__(self, params):
        super().__init__(params)               # params arrive from config.yaml

    async def on_event(self, event):
        # event.name / event.ts / event.payload — an emission-time snapshot
        # event.ctx — the live RequestContext (final state by delivery time)
        await post_to_slack(event.payload)
```

- **Observers** (default) are fire-and-forget: events queue for a background dispatcher, so a slow or crashing handler can never add latency to (or fail) a request. Handler crashes are logged and counted (`gateway_events_total{...,outcome=error}`); a full queue drops (`outcome=dropped`) rather than blocking; shutdown drains everything pending.
- **Interceptors** run in-band — their latency *is* request latency, that is the trade — and may `raise GatewayError(...)` to abort the request through the normal error path (log row, metrics, client envelope; any other exception is swallowed, logged, counted). They may only subscribe, explicitly by name, to the two events emitted before the request becomes irreversible: `request.received` (after auth+parse, before any policy) and `response.completed` (final body, pre-client). Interception is inspect + veto: handlers see the event but never mutate content — mutation stays guardrails' job (one source of truth).
- **With no handlers configured the bus is inert** — every emission is a no-op lookup. Enable the machinery, pay nothing until you subscribe.

Event catalog:

| Event | Fired | Payload highlights |
|---|---|---|
| `request.received` | after auth+parse, before any policy check — *interceptable* | `model`, `stream`, `key_name` |
| `request.rejected` | blocked — 4xx (policy, guardrail, rate limit, budget, interceptor veto) | `error`, `status_code` |
| `request.failed` | errored — 5xx / upstream exhaustion | `error`, `status_code` |
| `request.cache_hit` | exact or semantic cache replay, or an idempotency-key replay | `kind` (`hit` / `semantic` / `idempotency`) |
| `request.routed` | deployment selected (stream and non-stream) | `deployment`, `provider`, `native_model`, `stream` |
| `response.completed` | final non-streaming response, post-guardrails, pre-client — *interceptable* | `response`, `usage`, `cost_usd`, `surface` (embedding / images / transcription) |
| `stream.started` / `stream.completed` | first chunk / end of a streamed body (any outcome) | `model` / `status` |
| `auth.rejected` | credential rejected at the edge | `path`, `reason` |
| `key.created` / `key.updated` / `key.rotated` / `key.revoked`, `tenant.*`, `breaker.reset` | admin actions — the audit choke point, same names and payload as the audit webhook | `event`, `actor`, `details` |
| `runaway.trip` | the loop detector jailed a key (identical-request threshold reached) | `key_name`, `model`, `count`, `threshold`, `window_seconds`, `cooldown_seconds`, `offenses` |
| `runaway.released` | admin released a key from the runaway jail (audit choke point) | `key_id`, `actor` |
| `concurrency.timeout` | a queue wait for a concurrency slot expired — 429 + Retry-After | `key_name`, `model`, `max_concurrency`, `wait_seconds`, `waited_seconds` |
| `budget.threshold` | budget alert fired (per crossed level) | `key`, `key_name`, `level`, `spend_usd`, `budget_usd` |
| `expiry.threshold` | key-expiry warning fired (per crossed day-level; observer-only) | `key`, `key_name`, `level_days`, `days_remaining`, `expires_at` |
| `feedback.recorded` | client rated a request | `request_id`, `model`, `rating`, `key_name` |
| `canary.failed` | a synthetic probe failed (per failed probe — count or dedupe on your side) | `deployment`, `provider`, `model`, `outcome` (`error`/`timeout`), `error` |
| `canary.recovered` | a deployment's probe returned to ok after a failure | `deployment`, `provider`, `model`, `latency_ms` |
| `pii.found` | a scanned log row's stored bodies matched PII/secret detectors | `log_id`, `request_id`, `key_name`, `model`, `kinds` |
| `batch.created` / `cancelled` / `completed` / `failed` / `deferred` / `expired` | batch lifecycle | `batch_id`, `key_id`, line counts, output/error file ids |

Per-chunk stream events are deliberately absent — the hot path only ever carries the two coarse stream events. Deliveries count on `gateway_events_total{handler,event,outcome}` (delivered / dropped / error / aborted). The `events:` config is hot-reloadable (handlers swap live; `queue_size` applies at restart). 20 tests pin the contract (`tests/test_events.py`).

## Feedback

Clients rate any logged request — the missing half of the quality story. Shadow mirroring and prompt A/B compare *outputs*; feedback is the signal that says which one was actually good, and it's what turns `/admin/stats` latency dashboards into quality dashboards.

```bash
# The client rates by the id the gateway already returned:
curl -X POST http://localhost:8000/v1/feedback \
  -H "Authorization: Bearer sk-gw-..." -H "Content-Type: application/json" \
  -d '{"request_id": "req_...", "rating": 5, "comment": "exactly right"}'
# → { "id": "fb-...", "object": "feedback", "request_id": "req_...", ... }
```

- **Any logged request is rateable** — success, stream, or failure (the client may want to rate the failure itself); the id is the `X-Request-Id` response header.
- **Ownership follows the files/batches rule** — the request-log row must belong to the calling key (the platform admin may rate anything); foreign and unknown ids get the same 404, never confirming another key's request exists. Feedback rides the `chat` endpoint scope.
- **Multiple signals per request are allowed** — a client may correct a rating.
- **Denormalized key/model/tenant** — captured from the log row at feedback time, so stats stay stable if a model is later deleted or remapped, and aggregates never join.
- **Admin views**: `GET /admin/feedback` (filterable list) and `GET /admin/feedback/stats` (overall / by-model / by-key buckets; positive = rating 4–5, negative = 1–2; tenant admins see their own tenant only). Ratings count on `gateway_feedback_total{rating}` and surface as `feedback.recorded` lifecycle events.

11 tests pin the contract (`tests/test_feedback.py`).

## Canary probes

The gateway only knows a deployment is healthy when real traffic succeeds through it — a dead fallback that never receives traffic reads as "fine" until the moment it's needed, and a shadow deployment only ever sees mirrored traffic. Synthetic canary probes close that gap: a background loop sends a tiny chat request straight to each deployment's provider on a schedule, so upstream health is *measured*, not inferred.

```yaml
canary:
  enabled: true            # off by default — probes spend real quota
  interval_seconds: 300    # one wave per interval
  timeout_seconds: 10      # each probe carries its own clock
  max_tokens: 1            # the entire cost of a probe
  prompt: "ping"
  # deployments: ["openai/*"]   # fnmatch allowlist; unset = all (shadows included)
```

- **Direct adapter calls** — probes use the same wire the router would (`adapter.complete`), but none of the request machinery: no pipeline, no guardrails, no cache, no request-log rows, no spend accounting, no circuit-breaker accounting. They can neither affect client traffic nor be mistaken for it in logs/stats, and they never half-open a breaker.
- **Every configured deployment is probed** — primary, fallback and shadow deployments alike; the deployments real traffic rarely reaches are exactly the ones whose health traffic can't measure. An `fnmatch` allowlist (`deployments:`) trims the fleet if some deployments are too expensive to ping.
- **Outcomes**: `ok` / `timeout` (provider never answered within `timeout_seconds`) / `error` (answered badly — HTTP error, bad JSON) / `unknown` (never probed). Counted on `gateway_canary_probes_total{deployment,provider,outcome}`.
- **Transitions, not spam**: every failed probe emits a `canary.failed` event; a deployment returning to ok emits `canary.recovered` — the two signals an on-call actually needs (subscribers count or dedupe as they like).
- **Admin**: `GET /admin/canaries` (fleet view: outcome, latency, last check, plus config echo) and `POST /admin/canaries/probe` (run a wave now, regardless of `enabled`). Both platform-admin-only — deployment health is platform configuration, like `/admin/routing`. Hot-reloadable: `enabled` / interval / timeout / allowlist apply from the next loop iteration (the loop always runs, idling at a 30s re-check when disabled, so enabling takes effect immediately).

8 tests pin the contract (`tests/test_canary.py`).

## PII scan

An audit that answers "did secrets or PII land in the request log?" Clients paste API keys into prompts; guardrails may be off, in observe mode, or only masking the *upstream* copy — the log keeps the **original as received** (`ctx.request_body` is captured before the guardrail phase), so stored rows are where exposure actually shows. Redaction protects the provider; this tells you what your retention policy is holding.

```bash
# Bulk: scan the last 100 rows (bounded by pii_scan.max_rows)
curl -H "Authorization: Bearer sk-gw-admin-..." \
  "http://localhost:8000/admin/pii-scan?limit=100"
# → { "scanned": 100, "with_findings": 2, "bodies_not_captured": 0,
#     "by_kind": {"api_key": 1, "jwt": 1}, "rows": [...], "kinds": [...] }

# One row (kind + count only — never the matched text)
curl -H "Authorization: Bearer sk-gw-admin-..." \
  "http://localhost:8000/admin/logs/42/pii-scan"
```

- **Detectors mirror the `pii_redaction` guardrail** — the same compiled patterns, imported from it, so "masked" and "found" can never drift — plus secret shapes a redactor wouldn't mask (JWTs, private-key blocks), all Luhn-validated credit cards. Custom detectors: `pii_scan.extra_patterns` (name → regex); drop noisy built-ins with `exclude_kinds` (e.g. `ipv4`).
- **Findings are kind + count only** — never the matched text. The audit must not become its own leak: a report that quotes the API key it found is a report full of API keys.
- **Clean ≠ not captured**: rows written with `logging.store_bodies: false` report `captured: false` and count under `bodies_not_captured` — the report never claims "safe" for rows it could not see.
- **Pure read**: scans are idempotent GETs, nothing persisted. Flagged rows count on `gateway_pii_scan_findings_total{kind}` (rows containing the kind, not raw occurrences) and emit `pii.found` events; tenant admins are scoped to their own tenant exactly like `/admin/logs`.
- **Caveats**: stored bodies are truncated at `logging.max_body_chars` — a secret past the boundary is invisible; and `store_bodies: false` is the only mode where nothing can leak into storage in the first place (this audit is for the mode where bodies are kept).

10 tests pin the contract (`tests/test_pii_scan.py`).

## Idempotency

Networks drop responses, clients time out, retries happen — and without idempotency a retried POST generates (and bills) twice. Send an `Idempotency-Key` header on any chat-family surface and the *successful* response is stored under (caller, key); retries replay it instead of re-executing:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-gw-..." \
  -H "Idempotency-Key: checkout-order-42" \
  -d '{"model": "gpt-4o", "messages": [...]}'
# first call:  X-Gateway-Cache: MISS        (billed normally)
# retry:      X-Gateway-Cache: IDEMPOTENT   (same body, zero cost, no upstream call)
```

- **Only successes are stored** — errors and guardrail blocks re-execute on retry, which is exactly what a transient failure needs. Streamed successes store the synthesized full response; a stream that dies mid-flight stores nothing.
- **No double billing**: the stored usage was billed at first execution; replays are zero-cost `cache_hit` log rows (same contract as content-cache hits — `status: "success"` + `cache_hit: true`) and don't touch the caller's spend.
- **Key reuse with different parameters is a 400** (`idempotency_key_reuse`): the fingerprint is the original request as received — the same normalized identity the response cache keys on. Changing the request under the same key gets a refusal, not a wrong answer.
- **In-flight duplicates dedup**: the canonical case is retrying while the original still runs (client-side timeout). The retry waits for the original — bounded by `inflight_wait_seconds`, never cancelling it — then replays it; a wedged original times out and the retry executes itself (last writer wins). This map is in-process: with the Redis backend, *completed* entries dedup across workers/replicas, but two requests racing simultaneously on different replicas both execute.
- **Entries are private per caller** — keyed by (calling key, client-chosen key); two clients may independently use the same key value. Keys are 1–255 non-whitespace characters (invalid → 400 `invalid_idempotency_key`).
- **Runs before budget**: a retry serves even if the caller's ceiling was crossed after the original was authorized — the original execution was already permitted. New requests under the same key still hit the ceiling.
- **Scope**: every chat-family surface — `/v1/chat/completions`, `/v1/responses`, native `/v1/messages`, native Gemini `:generateContent` — the replay comes back in the surface's own wire format. Embeddings/images/audio and batch lines ignore the header. No header = zero behavior change; `idempotency.enabled: false` ignores the header entirely.

Config lives in the `idempotency:` section: TTL (default 24h — Stripe's idempotency window), LRU size, in-flight wait, and the backend (`memory` or `redis`, same selection switch as the response cache — Redis entries are namespaced apart at `<prefix>:idem:cache:*`). Outcomes count on `gateway_idempotency_total{outcome}` — `replayed` / `stored` / `rejected` — and replays emit `request.cache_hit` with `kind: "idempotency"`.

14 tests pin the contract (`tests/test_idempotency.py`).

## Context-window management

A request that can't fit the model's context window wastes everyone's time *and* the provider's quota: it fails upstream with a provider-shaped 400 after the gateway has already metered, routed and logged it. Declare the window once per public model and the `context_window` guardrail rejects (or redirects) over-window requests *before* any of that:

```yaml
models:
  - name: gpt-4o
    context_window: 128000   # total tokens: input + output
    deployments: [...]

guardrails:
  - name: context_window
    enabled: true
    params:
      reserve_output_tokens: 1024
      on_exceed: block         # block | redirect
      redirect_model: gpt-4o    # for on_exceed: redirect — a *bigger* model
```

- **The window is model metadata**, not a guardrail param: `models[].context_window` is declared once per public model (surfaced at `/admin/routing`), resolved onto the request at model resolution, and re-resolved when another guardrail redirects mid-chain. Guardrail params are policy: `window` forces a specific number (testing, or managing a model that declares none), `default_window` governs models without a declaration, and `reserve_output_tokens` is the response budget assumed when the caller doesn't set `max_tokens`.
- **OpenAI-style refusal, with the actual numbers**: `This model's maximum context length is 1000 tokens. However, your messages were estimated at 401 tokens plus max_tokens=5000 — 5001 total. Reduce the length of the messages or the requested output.` The caller can fix the request without a support ticket.
- **`on_exceed: redirect`** is reject-and-redirect in the other direction: "this document doesn't fit the small model, serve it on the big one." The pipeline re-checks the calling key's allowlist for the target (fail closed), and everything downstream — routing, logs, cache identity — sees the final model.
- **The estimate is the same ~4-chars/token heuristic the rate limiter uses** (TPM), computed on the outgoing message list — the managed system prompt is counted (it's part of what goes upstream). Strictly-greater blocks: `input + max_tokens == window` fits. Image/audio parts count as their `[image]`/`[audio]` placeholders (a documented under-count, same trade-off as TPM); the provider's own 400 remains the final word at the boundary.
- **Scope**: every chat-family surface (`/v1/chat/completions`, `/v1/responses`, native `/v1/messages`, native Gemini) — blocked requests 400 pre-dispatch, streams included; embeddings/images/audio are unaffected. Like any guardrail it composes with observe mode, scoping and per-key `disabled_guardrails`.

14 tests pin the contract (`tests/test_context_window.py`).

## Prompt compression

`context_window`'s remediation counterpart: where that guardrail *blocks* (or redirects) an over-window request, `prompt_compression` **compresses it to fit** — deterministically, in the request phase, with no LLM call and no added latency. Long agent conversations keep working instead of dying at the window; every saved token is also a saved cent. Order it **before** `context_window` in the guardrails list:

```yaml
guardrails:
  - name: prompt_compression
    enabled: true
    params:
      # max_input_tokens: 8000   # explicit budget; empty = window-fit
      reserve_output_tokens: 1024
      keep_first: 2              # messages kept at the top (the task)
      keep_last: 4               # at the bottom (the live exchange)
      min_message_chars: 512     # floor for oldest-first truncation
  - name: context_window         # ordered after: gets what compression saved
    enabled: true
```

The algorithm is middle-out folding, the shape agent frameworks converged on:

- **Fold the middle.** Leading *system* messages are always kept (policy, small, load-bearing), plus the first `keep_first` and the last `keep_last` non-system messages; everything between is dropped. A short note — `[gateway: 6 earlier messages (~2400 tokens) were removed...]` — is prepended to the first kept message so the model knows its history has a gap. Content-only: roles and content shapes are preserved (a parts-list message gains one leading text part, images ride along untouched), so every provider adapter and every native outbound format handles the folded request exactly like the original.
- **Then truncate oldest-first**, only if still over: kept non-system messages are shortened toward `min_message_chars` one at a time, oldest first. The system block and the *last* message are never trimmed — a response to a mangled task or a mangled latest turn is garbage either way.
- **Never blocks.** When even compression can't reach the target (a single gigantic last message), the guardrail steps aside and the request flows on untouched — `context_window` (or the provider) gives the honest error. Compression is a saver, not a blocker.

The target: `max_input_tokens` when set (an explicit budget — cost control even on models whose window would never block), else the model's declared window minus `reserve_output_tokens` — the same vocabulary and default `context_window` uses, so a request folded under the target then *passes* the `context_window` check ordered after it. Models with neither are unmanaged.

Like every guardrail it composes with the framework for free: observe mode logs the would-be savings without touching the request (measure the ROI before enforcing), scoping limits it to keys/models/routes, per-key `disabled_guardrails` turns it off at runtime, and each verdict lands in the request log's `guardrail_actions` with `tokens_before` / `tokens_after` / `tokens_saved` / `messages_dropped` / `messages_truncated`. Enforced savings aggregate on `gateway_prompt_compression_saved_tokens_total{model}` — the ROI metric; multiply by your input price. Modifications run before the cache lookup, so compressed and uncompressed forms of one logical prompt hold different cache entries (different wires, different entries).

18 tests pin the contract (`tests/test_prompt_compression.py`).

## Capture-replay harness

Every hot reload changes policy. Today you eyeball the YAML, apply it, and hope. The harness closes the loop: the request log already records real traffic (bodies, caller, route, guardrail verdicts, routing, outcome) — **capture** that traffic, **replay** it against a candidate config, and **read the diff** before clients ever see it.

```bash
# 1. Capture: the replayable request log as JSONL (rows with stored
#    bodies; shadow mirrors excluded; same filters as /admin/logs)
curl -H "Authorization: Bearer $ADMIN_KEY" \
  "http://localhost:8000/admin/logs/export?status=success&limit=100" > capture.jsonl

# 2. Apply the candidate config (hot reload) — or don't: replay works
#    against whatever is live.

# 3. Replay + diff: re-executes each captured request through the
#    request-policy phases ONLY, as the recorded caller's CURRENT key.
curl -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"entries\": $(cat capture.jsonl | python3 -c 'import sys,json; print(json.dumps([json.loads(l) for l in sys.stdin]))')}" \
  http://localhost:8000/admin/replay
```

The report, one result per line plus a summary:

```json
{
  "replayed": 100, "changed": 2, "unchanged": 95, "not_comparable": 3, "errors": 0,
  "results": [
    {
      "result": "changed",              // changed | unchanged | not_comparable | error
      "changes": ["status", "guardrail_actions"],
      "predicted": {                    // what the CURRENT config would do
        "model": "test-model", "status": "blocked",
        "guardrail_actions": [{"guardrail": "keyword_blocklist", "phase": "request", "action": "block"}],
        "prompt_policy_applied": true,  // reported, not diffed
        "deployments": ["openai/primary", "openai/fallback"],  // priority ladder, no dispatch
        "error": {"code": "content_blocked", "message": "..."}
      },
      "recorded": { "model": "test-model", "status": "success", "deployment": "openai/primary", "guardrail_actions": [] }
    }
  ]
}
```

What a replay is — and deliberately isn't:

- **Policy only, no execution.** Each captured request runs the *exact* code a live one does for policy — model resolution, prompt policy, permissions, the request-phase guardrails, redirect re-validation, routing selection (the priority ladder; weighted tiers pick randomly, so membership is the honest signal, not the single pick). No upstream call, no rate-limit/budget/idempotency counters (a retry would hit them; a replay must not), no cache, no spend, no request-log row, no lifecycle events.
- **Dry-run means invisible.** Guardrail verdicts are recorded on the replay result but never touch `gateway_guardrail_blocks_total` / `..._observed_total` — a harness run can never trip block alerts. Line outcomes count on `gateway_replay_total{result}`.
- **The caller is replayed at their *current* policy.** The key row is re-loaded fresh: a key edited or revoked since the capture replays under what it is *now* — a revoked key surfaces as `changed` (would now 401). That's the regression you're hunting.
- **Honest comparability.** Rows recorded as `error` export with a `skip_reason` (their outcome was upstream/counter state a dry run cannot re-run) and produce a prediction but no diff. Entries with no virtual-key identity — bare requests, admin-key captures — replay as the *calling* admin's context. Entries from another tenant are skipped (`entry belongs to another tenant`) — tenant admins replay only their own traffic, the same boundary as everywhere else.
- **The diff compares what policy decides**: status, model, request-phase guardrail verdicts (response/stream-phase verdicts can't be predicted — the replay stops at the request phase), deployment-ladder membership. Prompt-policy application is *reported* (`prompt_policy_applied`) rather than diffed, since the record predates it.

Config lives in the `replay:` section: `enabled` (default true — `POST /admin/replay` answers 404 `replay_disabled` when false) and `max_entries` (default 500, a 400 beyond — sample traffic, don't re-execute history). Both are read live off the running config.

16 tests pin the contract (`tests/test_replay.py`).

## Shadow diffing

Shadow mirroring writes *paired* rows — the client-facing primary and the mirrored candidate share a `request_id`, each with its own body, latency, tokens and cost — but until tier 35 nothing ever compared them. An operator weighing "move this model from openai to azure" had hundreds of pairs and no way to answer *how divergent is the candidate? at what latency/cost delta? show me the worst pairs.* Shadow diffing is that evidence layer:

- **Raw-vs-raw scoring, at mirror time.** Each mirror's answer is diffed against the primary's **raw provider text** — the pre-guardrail answer on the buffered path (extracted as an immutable string *before* the response phase runs), the accumulated source text on the streaming path — against the mirror's equally raw answer (mirrors run no guardrails by design). A rewrite on the primary leg therefore never reads as provider drift: the PII redactor can mask every client-seen body and the pair still scores `exact`.
- **Fixed verdict buckets, no config surface.** `exact` (similarity 1.0), `near` (≥ 0.80 — cosmetic drift: whitespace, punctuation, phrasing), `divergent` (different answers), `shadow_error` (the mirror call itself failed — a candidate that can't serve traffic is the loudest finding of all), `not_comparable` (no text on a side: tool-call replies, empty content — never a fake 0.0 that would read as maximal divergence). Similarity is `difflib`'s ratio: deterministic, dependency-free, and the raw score ships on every row so re-bucketing with different thresholds is possible after the fact.
- **Aggregation stays SQL.** Verdict and similarity are computed in the mirror task and written onto the shadow row (`shadow_verdict`, `shadow_similarity` — visible in `/admin/logs`), so `GET /admin/shadow-diff` is a `GROUP BY` over stored columns: per (model, shadow deployment) — pair counts, verdict breakdown, match rate, mean similarity over comparable pairs, and latency/token/cost deltas against the paired primaries. Scoring happens in memory, so verdicts exist even when `logging.store_bodies` is off; only the drilldown excerpts need stored bodies (it reports `null` text honestly in that case).
- **The drilldown is the review queue.** `GET /admin/shadow-diff/pairs?model=…&deployment=…&verdict=divergent` shows both sides side by side — the migration review queue. The admin dashboard's Routing tab renders the same: the evidence table under the routing table, with a pair dialog behind each row.
- **Honest scoping.** Deployment mirrors only — prompt A/B rows are also shadow rows but their divergence is *by design* (a different prompt was tested), so the aggregate excludes them; they still carry verdicts on the log surface for the prompt workflow. Mirrors scored before tier 35 (verdict NULL) are never re-guessed. And the endpoints are platform-admin-only, the same boundary as `/admin/routing`, because the pairs carry request bodies that cross tenants.

13 tests pin the contract (`tests/test_shadow_diff.py`).

## Candidate trials

Shadow mirroring + diffing is *passive* evidence: it only covers traffic the model actually gets, only candidates wired as `shadow: true`, and only at the rate real requests arrive. When a migration decision needs evidence **now** — or the candidate isn't wired as a shadow deployment — a trial replays the past deliberately. `POST /admin/trials` takes a captured traffic window (one public model's logged chat requests — the same request-log rows `GET /admin/logs/export` exports) and sends each one to a named candidate deployment with **real upstream calls**:

- **The candidate can be any configured deployment.** It does not need to belong to the replayed model — the adapter maps the request to the candidate's upstream model, which is exactly how migrations work (a candidate usually runs a *different* model). Selection takes the model's successful, body-storing, client-facing chat rows inside `since`/`until`, newest first, up to `limit`.
- **Throttled and spend-capped.** Entries replay in waves of at most `concurrency` parallel calls; between waves the runner re-reads the run row and stops before the next wave when cumulative candidate spend reaches `max_cost_usd`. All three per-run values are clamped to the `trials:` ceilings (`max_entries` 200, `max_concurrency` 2, `max_cost_usd` $1.00 by default) — asking for more just runs at the ceiling. The cap is a *between-wave* cap: it can overshoot by at most one wave, and never aborts calls in flight.
- **Mirror-like execution.** Trial calls skip guardrails, rate limits, budgets, the caches, lifecycle events and metrics — and even *breaker bookkeeping*: an experiment must not trip a serving candidate's circuit. Nobody's spend moves: trial rows carry their own cost (the run's `cost_usd` totals it) but no key or tenant is billed. The run row plus the scored log rows are the evidence surface.
- **Scored like mirrors, honestly.** Each replayed entry writes a shadow request-log row sharing the original's `request_id` (plus a `trial_id`), scored with the tier-35 verdict vocabulary. One difference is forced by the persistence boundary: the primary's *raw* provider text is not stored, so trials score against the stored body — and when the primary was rewritten by a response/stream guardrail (its stored body is the client-seen text), the pair is `not_comparable` instead of fake divergence.
- **The standing evidence stays clean.** Trial rows are excluded from `/admin/shadow-diff` (its scope is live mirroring, `trial_id IS NULL`); `GET /admin/trials/{id}/pairs` is the trial's own drilldown — the same pair shape, verdict-filterable — and `GET /admin/trials` lists runs with progress, spend and the verdict summary folded into each row.
- **DB status is the only truth.** Claim, progress, cost and finalization are guarded `UPDATE`s on the row's current status; the runner re-checks between waves, so `POST /admin/trials/{id}/cancel`, another replica, or the startup sweep (a process died mid-run → the row is marked `orphaned_by_restart`-cancelled at boot; a live run on another replica sees the cancel and stops within one wave) always win. Stop reasons say why a run ended: `done`, `no_entries`, `cost_cap`, `cancelled`, `orphaned_by_restart`, `error`.

All five endpoints are platform-admin-only — the pairs carry request bodies, which cross tenants (the same boundary as `/admin/routing` and `/admin/shadow-diff`). The admin dashboard's Routing tab has the "Candidate trials" panel: pick the model's captured window and the candidate, watch the run, drill into the pairs in the same dialog as mirror pairs.

15 tests pin the contract (`tests/test_trials.py`). Config lives in the `trials:` section: `enabled` (default true — `POST /admin/trials` answers 404 `trials_disabled` when false) and the three ceilings.

## Migration verdicts

Tiers 13/35/36 collect the evidence — mirrored pairs scored raw-vs-raw, candidate trials replayed on demand. What was still missing was the *call*: an operator staring at "match rate 96%, mean similarity 0.91, 2.4% shadow errors, +18% latency" had to judge by feel whether the candidate is promotable. Tier 37 turns the evidence into a recommendation — auditable, never authoritative:

- **One engine, both evidence legs.** `core/migration.evaluate` is a pure function over one evidence group — the exact shape a `/admin/shadow-diff` mirror group *and* a trial run's summary carry — so `GET /admin/migration/verdicts` (mirrors) and the recommendations folded into every `/admin/trials` row (trials) verdict identically through one code path. No state, no clock: every response echoes the thresholds it applied.
- **The vocabulary.** `go` — every bar cleared. `hold` — answers acceptable, a soft concern needs a business call (a marginal similarity mean, a latency regression measured as a **percent of the primary's mean**, because an absolute bar means different things on a 200ms and a 20s model). `no_go` — answers diverge (match rate) or the candidate *fails the traffic it is given* (shadow-error rate). `insufficient_evidence` — too thin to judge. The worst honest call wins: a hold never outranks a no_go.
- **Sufficiency gates both volumes.** `min_pairs` (20 by default) applies twice: to total pairs — the volume a shadow-error rate can be measured on — and to comparable pairs — the volume answer equivalence can be judged on (not_comparable rows prove nothing about it). One deliberate exception: a shadow-error rate over its bar on a full volume of pairs is a `no_go` even when nothing was comparable — a candidate that fails every call it is given has already answered the question.
- **Nothing is faked.** Unmeasurable checks (missing latency means, a 0 primary mean) report null and are skipped, never passed; the raw numbers ship beside every verdict as `checks` (value + threshold + pass) and `reasons` carries one line per failed check — so a recommendation can be audited, re-bucketed or second-guessed, never taken on faith.
- **Hot-reloadable bars.** All five thresholds live in the `migration:` config section and are read live: tighten a bar, reload, and the next read re-verdicts — with the thresholds echoed in the response, the verdict is always auditable against the bars that produced it. A recommendation is *decision support*, not a gate: nothing promotes automatically (the promotion action itself is a deliberate, audited step).

The endpoint is platform-admin-only, like the evidence it reads; `?since=` bounds the window (stale pairs — from before a candidate's last update — can be excluded). The admin dashboard's Routing tab renders the recommendation as a "Go / no-go" badge on each shadow-diff row and each trial run, tooltip the failed-check reasons. 17 tests pin the contract (`tests/test_migration.py`).

## Migration promotion

Tier 37's go verdict was still missing its act: an operator reading "go" had to edit config.yaml by hand — drop the `shadow:` lines, pick a weight, align the priority tier, reload, and hope no typo took the gateway down mid-edit. Tier 38 makes that edit deliberate and safe, in two steps:

- **`POST /admin/migration/plan` — preview, no side effects.** Renders the exact config.yaml line edits for promoting a `deployment` into a `model` at `weight` (1–100, default 10), plus the evidence verdict justifying them. The response carries the promotion `mode`, a structured `changes` list, a unified `diff` of the file, the resulting `canary_share` of the serving tier's weight, `warnings`, the `recommendation` and its `evidence_source` (the standing mirror group when one exists, else the latest completed trial for the pair, else none — thin evidence is honestly labeled). The file is never written, nothing is audited.
- **`POST /admin/migration/promote` — apply.** Re-builds the plan from the file *at apply time* (a preview is never applied stale), gates on the evidence verdict, then: timestamped backup → write → the same hot-reload path `POST /admin/reload` takes → audit `promotion.applied`. If the reload refuses, the backup is restored and the running config never moved — the operator gets a 400 saying exactly that.

The semantics are canary-shaped, and nothing is ever auto-removed:

- **`shadow_flip`** — the shadow twin's `shadow:`/`shadow_fraction:` lines are dropped and it starts serving at the requested weight **inside the incumbent's priority tier**. The incumbent is never demoted or removed: promotion only adds serving capacity. Promote again at a higher weight to shift more traffic — the canary ladder a manual edit would climb.
- **`rebalance`** — an already-serving deployment's weight changes. Asking for the weight it already has is an honest no-op.
- **`cross_model_insert`** — a candidate living on another public model (typically where a trial generated the evidence) is inserted as a block-form *copy* into the target model's deployments, aligned to its serving tier; the original is untouched and keeps serving (or mirroring) its own model.

The gate: a `no_go` verdict (answers diverge, or the candidate fails the traffic it is given) is a **409 `promotion_blocked`** with the reasons in the message, unless `force: true` — a forced promotion applies and is audited as forced. A `hold` or `insufficient_evidence` verdict does not block; the concern rides along as a warning on the promotion.

The text surgery is honestly bounded: it only edits config.yaml's canonical **block form** (dash line + indented `key: value` lines). Comments between entries survive byte-for-byte; flow-style entries and a file that disagrees with the running config are refused with a clear 409 — never best-effort-edited. config.yaml stays the single routing truth: there is no DB overlay, no side state — the file, the reload and the audit trail all tell the same story. The admin dashboard's Routing tab puts a "Promote…" button on every shadow-diff row, opening a dialog that re-plans on every weight change (the diff *is* the preview) and applies with the same backup → reload → audit flow. Both endpoints are platform-admin-only. 16 tests pin the contract (`tests/test_promotion.py`).

## Promotion rollback

Every apply — promote or rollback — leaves a timestamped `config.yaml.bak-<utc stamp>` next to config.yaml. Tier 39 turns those files from operator folklore into the undo surface:

- **`GET /admin/migration/backups`** — list, newest first (the stamp in the name sorts chronologically), a pure read. Each row *explains* the backup: `created_at` (the stamp, not the filesystem clock), `size_bytes`, whether it still **parses** (an old backup can predate schema changes — honestly labeled, refused at apply time), `is_current` (byte-identical to the live file — a rollback to it is the no-op the apply endpoint refuses), and the structured **`changes`** restoring it would apply, computed by comparing the parsed configs: `shadow`/`unshadow` (a promotion's flip, undone — the candidate goes back to mirroring), `set_weight`/`set_priority` with `from`/`to`, `add_deployment`/`remove_deployment` (a cross-model insert, undone).
- **`GET /admin/migration/backups/{name}`** — the same row plus the line-level `diff` of the live file → the backup's text: the human preview of what rolling back applies. Flat names only — a path separator never passes validation, so the endpoints can never reach outside the config file's directory.
- **`POST /admin/migration/rollback`** — apply, the same discipline in reverse. The chosen backup (`backup` names it; omitted = the latest, the common "undo the last promotion") **must parse** — never start an edit that cannot land; a byte-identical one is refused with 409 `rollback_noop`; the live file is backed up **first** (the rollback's own undo point), then the backup's text is written, the same hot-reload path runs (source `rollback`), and the act is audited as `promotion.rolled_back` with the structured changes. On reload refusal the undo point is restored and the running config never moved.

The semantics that fall out: **a rollback is one rollback away from undone, in both directions.** After a rollback, the *latest* backup is the rollback's own undo point — the promoted state — so rolling back again with no body *redoes* the promotion (pinned by a test). There is no evidence gate and no force flag: rollback *is* the escape hatch — but a deliberate, audited one, never automatic. The admin dashboard's Routing tab has a Backups button next to the shadow-diff table: one row per backup with its changes story, a Preview of the diff, and a confirm-guarded Rollback-to-this that toasts the undo point. All three endpoints are platform-admin-only. 9 tests pin the contract (`tests/test_rollback.py`).

## Post-promotion watch

Promotion ended the mirrors: the candidate serves, the evidence stops, and the operator is back to watching by feel — exactly when watching matters most. Tier 40 turns the serving logs into the continued story. `GET /admin/migration/watch` re-verdicts **what the logs still say**: one row per serving (non-shadow) deployment, judged by the same `migration:` thresholds with the same go / hold / no_go / insufficient_evidence vocabulary the pre-promotion verdicts used:

- **The evidence is the attributed serving rows** — the client-facing rows logged once a dispatch *completed* on the deployment. Mirrors (shadow twins and prompt A/B), candidate-trial replays and cache replays never reach the watch: evidence, experiments and cache hits are not serving traffic.
- **The attribution is the honest part, and it cuts both ways.** A request that fails *before* any dispatch completes — the router exhausted its ladder, a sole deployment failing every attempt — logs its row with no deployment, so a failing candidate shows up as **traffic that ran away**, never as fabricated attributed errors. The row's share split says it loudest: `expected_share` is the deployment's weight's share of its priority tier, `actual_share` is its fraction of the tier's attributed requests in the window — a rescued candidate thins toward 0 while its expected share stays put. The error rows that *do* land on a deployment are failures after the dispatch completed — a stream that dies mid-flight — and those feed the error-rate check.
- **The window is measured on the traffic itself.** Each row watches from `since` — the deployment's first serving row, the promotion moment as traffic saw it; no side state, nothing to drift. The baseline aggregates the same-priority-tier peers over exactly that window: the same traffic regime, the same clock. `?since=` (ISO-8601) overrides every window for slice re-verdicts; `?model=` / `?deployment=` narrow; an unknown name is empty data, not an error. Thresholds are echoed live from the `migration:` config.
- **The checks, same shape as every verdict:** `requests` (the `min_pairs` sufficiency gate — a bad five-minute window is not a verdict), `error_rate` (the deployment's own, against `max_shadow_error_rate` — the same SLO the mirror verdicts judged by, measured on serving traffic), and `latency_delta_pct` (its mean latency as a percent of the peers' mean, against `max_latency_delta_pct` — null and skipped with a caveat when unmeasurable, never a fake 0%). A `no_go` outranks a `hold`; thin volume is `insufficient_evidence` regardless of the numbers.

The verdict is a **recommendation to keep serving or roll back** — the endpoint never pushes the rollback button itself; an operator reading `no_go` or `hold` on a freshly promoted candidate knows where the Backups button is. The admin dashboard's Routing tab carries a Post-promotion watch table: per deployment the go/no-go badge (failed-check reasons in the tooltip), the expected → actual share split, requests/errors, latency mean, the latency delta vs the tier, cost mean, and the watching-since column. 11 tests pin the contract (`tests/test_watch.py`).

## Admin playground

"What does a request from this key actually experience?" — before tier 41 the honest answer required curl, the raw key material and a lot of squinting at `/admin/logs` afterwards. The playground is the dashboard's own chat client, and it answers by **being the thing**: `POST /admin/playground/chat` (platform-admin-only) runs one chat completion through the real pipeline and returns the client-facing completion verbatim beside the diagnostics of what happened while producing it.

- **Impersonation, not simulation.** The body's optional `key` names a virtual key (the human label from `/admin/keys`), resolved through the same chain the JWT auth step uses — name → active/expiry checks → tenant → the `auth_context_from_key` snapshot. The request then carries that key's full policy reality: its model/tool allowlists, its budgets and residency requirements, its guardrail scopes and rate limits, and **its billing** — spend, log rows and usage exports accrue to the key exactly as live traffic would. Omitted, the calling platform admin's own (unrestricted) context runs the chat. In both cases every row and audit event carries `auth_method: "playground"`, the one thing separating playground traffic from the key's live traffic.
- **The real pipeline, unchanged.** The chat rides the same `route="chat"` surface as `/v1/chat/completions` (so guardrail route-scopes behave identically), through the same guardrails, router, retries, response cache (an identical repeat can hit it — and say so) and accounting. Errors pass through verbatim: a guardrail block, an exhausted budget, a rate-limit refusal or an upstream failure surfaces as the exact envelope a real client would receive. Non-streaming only — `stream: true` is an explicit 400: one request, one answer, one diagnostics panel.
- **The diagnostics are the request-log row the run just wrote** — the same one `/admin/logs` shows and the usage export bills: request id, key, model/deployment/provider, status (including `blocked`), tokens, cost, latency, cache-hit and the `guardrail_actions` verdicts. One honest source; the UI renders it beside the conversation.
- **The trail is the price of the grant.** An admin impersonating tenant keys is a sensitive probe, so every attempt — success, blocked or failed — is audited as `playground.chat` with the calling admin as the actor and the impersonated key, model, request id and outcome in the details.

The admin dashboard's Playground tab wires it up: model dropdown (from `/admin/routing`), key dropdown (from `/admin/keys`, empty = admin), max-tokens/temperature knobs, a client-side conversation list where each Send posts the whole history (a real multi-turn body), and the diagnostics panel — errors render there as the envelope the real client would see. 12 tests + a UI-surface pin (`tests/test_playground.py`, `tests/test_admin_ui.py`).

## Service level objectives (SLOs)

"What do we promise the people using this gateway?" — a latency bar in a design doc is a hope; the same bar verdicted against the request logs is an SLO. Tier 42 turns promises into config: `slos:` declares named targets — a metric, a ceiling, a rolling window, a sufficiency minimum, scoped to a model or one deployment — and `GET /admin/slos` (platform-admin-only) measures each over the client experience and verdicts it honestly.

```yaml
slos:
  targets:
    - name: chat-p95-latency
      model: gpt-4o              # public model name (exact)
      # deployment: openai/primary   # narrow to one deployment's attributed rows
      metric: latency_p95_ms      # error_rate | latency_p50_ms | latency_p95_ms |
                                 # latency_p99_ms | ttft_p95_ms (all lower-is-better)
      target: 800                # the ceiling (fraction for error_rate, ms otherwise)
      window_minutes: 60         # rolling window measured at read time
      min_requests: 20           # sufficiency gate — thin volume is not a verdict
    - name: chat-errors
      model: gpt-4o
      metric: error_rate
      target: 0.01                # 1% (failed + blocked over total)
  # Error budgets (tier 43): multiwindow burn-rate rules over the same
  # rows — the SRE workbook's canonical pairs by default. `burn: []` disables.
  burn:
    - name: fast-burn             # page: 14.4x the allowed pace over 1h,
      rate: 14.4                  # confirmed still burning over 5m
      long_minutes: 60
      short_minutes: 5
    - name: slow-burn             # ticket: 3x over 1d, confirmed over 2h
      rate: 3.0
      long_minutes: 1440
      short_minutes: 120
  # Alert delivery (tier 44): a background evaluator runs one verdict pass
  # per evaluation_interval_seconds and turns the numbers into transitions.
  evaluation_interval_seconds: 60
  alerts:
    webhook_url: https://hooks.slack.com/services/...   # or url_env: SLO_ALERT_WEBHOOK_URL
    timeout_seconds: 5
    max_attempts: 2
```

- **Metrics are ceilings** — everything counts downward: `error_rate` (failed + blocked over total, as a fraction), `latency_p50_ms`/`latency_p95_ms`/`latency_p99_ms` (end-to-end), `ttft_p95_ms` (over the requests that streamed; non-streamed rows have no first-token to measure).
- **Client experience, not deployment health.** Every non-shadow, non-trial row for the scope: cache replays **included** (a cached answer is a fast answer the client really received), and at model scope the unattributed error rows too — the client experienced the failure even when no dispatch completed to blame a deployment (the tier-40 attribution story). A deployment-scoped target measures only its **attributed** rows, exactly like the post-promotion watch.
- **Honest verdicts.** `met` (the ceiling holds), `breached` (it does not — the `reason` says the metric, the number and the bar), `insufficient_evidence` (fewer than `min_requests` requests in the window, or nothing streamed for a TTFT target — a quiet window is not a verdict, and an unmeasurable metric is no answer, never a fabricated 0).
- **The numbers beside every verdict.** Each row ships the full measurement — requests, errors, error rate, the latency percentiles, TTFT p95 — all from one pass over the same rows, so a verdict is auditable and re-bucketable, never taken on faith.
- **Error budgets (tier 43).** Every bar restated as a *bad-event allowance*: `error_rate` 0.01 allows 1% failed+blocked rows; `latency_p95_ms` 800 implies 5% of requests may exceed 800ms — the percentile and the tail allowance are one promise seen from two sides. Each row carries a `budget`: the bad count against the allowance, **burn rate** (observed / allowed), budget remaining, and a state — `on_track` (burn ≤ 1), `at_risk` (within allowance overall but a burn rule is firing — fine on average, spending fast *now*), `exhausted` (the window overspent; remaining goes negative), `insufficient_evidence` (the same honesty the bar verdict has).
- **Burn-rate rules alert on the pace, not the average.** A burst that would take days to move a rolling p95 still pages in minutes: each `slos.burn` rule is the Google-SRE multiwindow recipe — the **long window** establishes the burn is real (burn ≥ the rule's rate), the **short window** confirms it is *still* burning now. The two-window AND is the anti-flap: a burst that already ended reads `steady`, and nobody gets paged for what stopped. Every window carries its own sufficiency gate (`min_requests` scaled by the window's share of the target's window, floored at 1) — a quiet hour is not a fire alarm no matter how scary the fraction.
- **Zero-tolerance bars never invent an infinity.** A `target: 0` allowance has no rate to compute: any bad event reads `exhausted`, the burn rules say `insufficient_evidence` with the reason saying why, and burn/remaining stay `null` rather than a fabricated ∞.
- **Delivery: the verdicts get teeth (tier 44).** A background evaluator runs one verdict pass per `evaluation_interval_seconds` — the *same* `status()` the admin endpoint serves, so an alert is decided by exactly the numbers the dashboard shows — and turns it into the two transitions on-call consumes: **`slo.alert`** when a target starts firing (bar `breached`, a burn rule `burning`, or the budget `exhausted` — in that precedence) and **`slo.recovered`** when it is provably healthy again. Fire-once / re-arm per target: the state persists on the settings KV table, so a restart never re-pages; a target that stays firing stays silent; recovery is what re-arms it. The honesty rule that makes it safe to leave on: a quiet window (`insufficient_evidence`) is *unknown* — it holds the previous state, never firing and never un-firing, because an SLO that stops receiving traffic must neither page nor quietly clear one.
- **Best-effort delivery, never a blocker.** Webhooks retry once with backoff and count on `gateway_alerts_total{kind="slo",outcome=sent|failed|skipped}`; without a `webhook_url` the POST is a skipped no-op while the `slo.alert`/`slo.recovered` events still fire; `alerts.enabled: false` turns the evaluator off entirely — no state, no events — while measurement keeps serving. The payload carries the definition, the condition, the measurement and the human message, so the receiving end can route on any of them without calling back.
- **Measured at read time; delivered on a schedule.** No cached verdicts anywhere — a tightened bar re-verdicts on the next read and re-arms on the next pass; hot-reload applies from the next evaluation. The Overview panel marks targets whose alert already fired with a red **alerting** badge (condition and since in its tooltip) until they provably recover.

The admin dashboard's Overview tab carries an SLO panel above the traffic tables: objective, scope, metric, the bar, the measured value, the request count against the `min_requests` gate, a **Budget** column (remaining % colored by state, each burn rule's pace verdict in its tooltip, red badges for rules that are firing), the met/breached/insufficient badge, and a red **alerting** badge on targets whose alert already fired — hidden when no targets are configured. 30 tests + three UI-surface pins (`tests/test_slos.py`, `tests/test_slo_alerts.py`, `tests/test_admin_ui.py`).

## Billing & usage invoices

The usage export is chargeback *raw material* — one row per request. The invoice is the finance-side question: **what did each tenant consume this month, per key, per model?** `GET /admin/invoices` rolls the same request-log rows up into a month statement, with the monthly budget beside the spend so every statement doubles as a spend-vs-plan check.

```bash
curl -H "Authorization: Bearer $ADMIN_KEY" \
  "http://localhost:8000/admin/invoices?month=2026-10"   # month omitted = the current one
```

- **The billing lens is client-consumed usage.** Shadow mirrors never cost the tenant anything, and candidate-trial replays are deliberately re-run traffic — both excluded. Cache hits are **included**: a cached answer is a real served request, already billed at the cached rate the calculator recorded.
- **Three levels, every one sorted by spend** (the big numbers read first): line items per (key, model), keys per tenant, tenants on the platform — each carrying requests, errors, the token splits (prompt / completion / cached) and cost, with exact totals over everything.
- **Budget context everywhere.** Each key section carries its `monthly_budget_usd` and a `budget_pct`; each tenant section its pooled monthly budget the same way. No budget set → `null`, never a fake zero. Platform keys (no tenant) are a separate `unassigned` bucket.
- **Zero-spend keys still appear** — an unused key is a finops finding, not an absence — and **deleted keys keep their line items** under the tenant they lived in (no budget to compare against): log retention outliving the key row is the normal case, not an edge.
- **Calendar months in UTC.** `month=YYYY-MM`, default the current month. A future month is a 400 — an invoice for a period that has not happened must not pretend to be empty.
- **Scoping.** Tenant admins invoice their own tenant only (forced, even when they pass `?tenant_id=`); platform admins may filter `?tenant_id=` or read the whole platform; plain keys get the admin 403.
- **`format=csv`** — one row per line item in a stable 13-column order (`month, tenant_id, tenant_name, key_id, key_name, model, requests, errors, prompt_tokens, completion_tokens, cached_tokens, total_tokens, cost_usd`), served as an attachment named `invoice_<month>.csv` — the same statement the JSON tells, reshaped for a spreadsheet.

The admin dashboard's Billing tab renders the statement: a month picker, the CSV download, totals cards, and tenants / keys / line-items tables with the budget badge colored at the 80/100% thresholds. 9 tests + a UI-surface pin (`tests/test_billing.py`, `tests/test_admin_ui.py`).

## Finops anomaly flags

The invoice says what each key and tenant consumed this month. The follow-up a finops person asks while reading it: **is any of this unusual?** `GET /admin/anomalies` compares the last `finops.window_hours` (default 24) of traffic against each key's trailing baseline — the `finops.baseline_days` (default 14) immediately before it — over the same client-consumed lens the invoice bills (shadow mirrors and trial replays excluded, cache hits included). Four flag shapes, each a billing surprise in its own way:

- **`cost_spike`** — a key spending ≥ `spike_ratio` (default 3×) its baseline daily rate: the runaway-agent shape, the mispriced-model shape, the someone-pointed-a-loop-at-us shape.
- **`traffic_spike`** — the request-rate version: volume before it becomes money.
- **`traffic_stop`** — a key with real baseline traffic that went silent: the churn / broken-integration signal a spend dashboard structurally cannot show (nothing is a great price).
- **`first_seen_model`** — a model with zero baseline traffic that appeared in the window: something new is on the bill, and new line items are how invoices surprise people.

```yaml
finops:
  enabled: true            # false = the kill switch: an honest empty flag list, the invoice keeps serving
  window_hours: 24
  baseline_days: 14
  spike_ratio: 3.0
  min_baseline_requests: 10
```

The honesty rules (the SLO epic's, money edition):

- **A thin baseline is not a verdict.** Fewer than `min_baseline_requests` baseline rows means no spike and no silence, never a scary ratio on thin evidence — and a brand-new key cannot spike at all (nothing to compare: it appears on the invoice with its spend, not here with a fabricated verdict).
- **A zero-cost baseline with window spend** (free models, all-cached replays) fires with `ratio: null` and a message saying the baseline cost nothing — an honest undefined, never a fabricated infinity.
- **Every flag carries its own numbers** — window vs baseline requests and cost, the ratio where one exists, the window/baseline bounds in the envelope — so a flag is auditable without calling back.

Flags are informational: read at request time, no loop, no state, no delivery — error-rate health is the SLO epic's job (tiers 42–44); this is the money lens. Tenant admins see their own tenant only (forced, even when they pass `?tenant_id=`); platform admins may `?tenant_id=` filter or read the whole platform; plain keys get the admin 403. The dashboard's Billing tab renders the flags beside the invoice, the human message in the kind badge's tooltip — the panel hides itself when there are none (no flags is the good case), except when detection is off, which gets a visible say-so instead of an absence the operator could mistake for quiet. 8 tests + a UI-surface pin (`tests/test_finops.py`, `tests/test_admin_ui.py`).

## Spend forecasts

The invoice states the month so far; the flags say what about it is unusual. The finance question on the way to either: **where will this month land?** `GET /admin/forecasts` (`month=YYYY-MM`, the invoice's own month contract) projects the final bill per key and per tenant over the same client-consumed lens — two projections, because one honest number is not always possible:

- **Run-rate** — spend so far over the elapsed fraction of the month, scaled to the whole month. Straight-line and honest about being an average: a key that stopped spending three days ago still projects its old pace.
- **Recent pace** — the last 7 days' daily spend (bounded to the month — on day 2, "the last 7 days" is really 2 days, and divides by 2) carried over the remaining days on top of the spend already booked. The correction: a key that stopped projects flat, a key that accelerated projects the acceleration.

The **trend ratio** (recent daily over month-to-date daily) ships beside them — under 1 is slowing, over 1 is accelerating — and the **verdicts** (projected vs `monthly_budget_usd`: `over_budget` / `at_risk` ≥80% / `within_budget`) are decided by the **recent-pace projection when one exists**, `verdict_based_on` saying which. Both projections always ship side by side; a verdict is never a mystery about its inputs.

The honesty rules (the epic's, forward-looking edition):

- **A complete month is not forecast.** A past month serves its actual total as the projection (`month_complete: true`, remaining days 0, trend null) — the invoice *is* the final bill, and a projection of a finished month would be a lie told with extra steps. A future or malformed month is the invoice's 400.
- **A trend needs at least two days of month** — less is hours of evidence dressed as a line, so `trend_ratio` is null there (the projections still ship; `elapsed_days` rides in the envelope so the reader judges the day-one noise). A zero month-to-date average against recent spend is null too — never a fabricated infinity.
- **The clock context ships in the envelope** (`elapsed_days` / `remaining_days` / `days_in_month`) — every projection is recomputable from the response itself, which is exactly what the tests do.
- **No budget set → projected pct and verdict null** — an honest absence, never a fake zero. Every existing key still appears: an unused key projects $0 of its budget, which is information, not an absence; deleted keys project where they lived, with no budget to compare against.

Read at request time — no loop, no config of its own (the 7-day window is a module constant; deterministic math with nothing to tune; the state on this surface is tier 48's delivery state, not the math's). Tenant admins see their own tenant only (forced); platform admins may `?tenant_id=` filter; plain keys get the admin 403. The dashboard's Billing tab renders the forecast between the invoice and the flags — where will it land, then is any of it unusual — both projections in one cell with the other a tooltip away, and a complete month rendering its actuals with a "final" say-so. 9 tests + a UI-surface pin (`tests/test_forecasts.py`, `tests/test_admin_ui.py`).

### Budget forecast alerts

A forecast nobody reads is a spreadsheet — so the projections get teeth the same way the SLO verdicts did (tier 44): a background evaluator turns each pass — the same `forecast()` the dashboard serves, so an alert is decided by exactly the numbers it shows — into the two transitions on-call consumes. `forecast.alert` fires when a budgeted entity **starts trending badly**: the month-end projection over its budget (`trending_over`) or at risk ≥80% (`trending_at_risk`), at both levels (a key against its own budget, a tenant against the pooled one — paging on where the month is *going*, before the money is spent; actual spend crossings stay the threshold alerts' job). `forecast.recovered` fires when the projection is provably back within plan — often because the operator raised the budget, an honest fix that recovers honestly.

```yaml
forecast_alerts:
  enabled: true                 # false = the kill switch: no state, no events, no webhooks
  webhook_url: https://hooks.example.com/gateway   # or url_env: FORECAST_ALERT_WEBHOOK_URL
  min_elapsed_days: 2.0         # the early-month gate — a day-one straight-line never pages
  evaluation_interval_seconds: 300
```

The honesty rules, paging edition:

- **Fire-once per (month, entity), persisted** on the settings KV table — a restart never re-pages, and the month rides in the state key: **a new month is a new plan**, so a tenant that trended over in November is paged again in December if December trends the same way. Recovery is what re-arms within a month.
- **Hours of evidence never page**: before `min_elapsed_days` of month the pass holds everything — a day-one straight-line extrapolation is exactly the noise the trend rules refuse.
- **No budget means no bar**: sections without one never fire, and a budget removed while firing drops the state silently (nothing to recover to; a re-added budget starts cold).

Delivery is best-effort (bounded retries, `gateway_alerts_total{kind="forecast"}`); without a webhook the POST is a skipped no-op while the observer events still fire. `GET /admin/forecasts` carries each section's persisted `alert` state (firing, condition, since — `null` when never fired or recovered) and the delivery config echo; the dashboard shows a red "paged" badge beside the verdict while firing and a "budget alerts off" say-so when delivery is disabled. 11 tests + a UI pin (`tests/test_forecast_alerts.py`, `tests/test_admin_ui.py`).

### Projected budget enforcement

Alerts tell a human; enforcement tells the caller. The request-time budget gate has always had hard caps — `max_budget_usd` and the daily/monthly windows, key and tenant — but a hard cap is blind to pace: a key at $8 of a $10 monthly budget on day 2, burning at a month-end pace of $120, sails through until actual spend crosses $10 — then 429s with the money already gone. Tier 50 adds the **projected stance**, the forecast family's third leg (the dashboard shows where the month lands → the evaluator pages a human → the gate answers the caller):

```yaml
budget_enforcement:
  enabled: true          # off by default: hard-cap-only behavior is exactly preserved
  min_elapsed_days: 2.0  # the honesty gate — hours of evidence never deny
```

With the stance on, a key or tenant whose month-end **run-rate projection** exceeds its `monthly_budget_usd` is denied with the same `429 insufficient_quota` the hard caps use — the caller cannot and should not distinguish the shapes — but the message says it is the projection, with the numbers: *"Virtual key 'trending' is projected to spend $10.29 by month end (day 17.5 of 30), over its $10.00 monthly budget ($6.0000 spent so far)."*

The rules, in order:

- **The hard caps are the floor, and they fire first.** A projection denial only ever happens with spend still under budget — actual over-budget spend gets the original "has exceeded its monthly budget" wording, so the two messages can never lie about each other.
- **The projection reads the counter lens, not the billing lens** — the auth-time `monthly_spend_usd` snapshot the hard cap just read, over elapsed days, scaled to the month. The gate and the cap can never disagree about what "spent" means, and the check costs no extra query on the hot path (which is also why it's the straight-line run-rate rather than tier 47's recent-pace correction: the recent window would need a per-request query, and the evaluator's cached verdicts are alert transitions, fire-once semantics — the wrong shape for a gate).
- **It self-heals.** Spend over elapsed days is a ratio: a caller that genuinely stops spending watches its projection decay toward the actual as the month elapses, falls back under its budget, and traffic resumes — no operator action. The hard cap stays the wall it always was; the stance is a trend, and trends end.
- **The honesty gate mirrors the family** (tiers 47/48): before `min_elapsed_days` of month, a day-one straight-line extrapolation is hours of evidence dressed as a trend and never denies.
- **Zero spend projects zero and passes; a stale bucket is last month's spend** — the stance composes with the window rollover, not against it. **No budget means no bar**, at either level.
- **Key and tenant legs share one rulebook** (`_check_projected_budget` — one helper, like every two-level check in the gate), and the stance applies on every surface the gate already runs: chat (all four inbound formats), realtime (per create), embeddings, images and audio. MCP stays budget-free by design (no tokens exist there).

16 tests (`tests/test_budget_windows.py`): the pure math (month shapes from 28 to 31 days, the month's first instant), the error wording at both levels, the gate rules against a pinned month clock (trending key and tenant denied with the numbers, within-budget passing, the honesty gate holding a would-be $120 projection, stale buckets counting zero, the hard cap firing first, the self-heal decay), and the traffic paths (off-by-default at both the gate and HTTP, an adaptive HTTP denial that holds on any day of any month, the honesty gate through HTTP, no-budget, stale-bucket, hard-cap precedence).

## Cost-aware routing & cached-token billing

Providers discount prompt-cache reads (OpenAI `prompt_tokens_details.cached_tokens`, Anthropic `cache_read_input_tokens` at 10% of base input, Gemini `cachedContentTokenCount`). And a fleet of deployments often contains the same capability at wildly different prices. Two features make the gateway bill and route accordingly:

**Cached-token billing.** Adapters normalize every provider's cached-token report into the OpenAI wire shape (`usage.prompt_tokens_details.cached_tokens`, always a subset of `prompt_tokens` — Anthropic's out-of-band cache reads fold into the prompt count). The calculator bills the split: `(prompt − cached) × input + cached × cached_input + completion × output`, with `cached_input` a per-model pricing key that **defaults to 50% of input** (OpenAI's discount — set it explicitly when the provider's differs; the built-in table encodes Anthropic's 10% and Gemini's ~25%). The split surfaces everywhere: the OpenAI response's usage details, native Anthropic `cache_read_input_tokens`, native Gemini `cachedContentTokenCount`, the request-log row (`cached_tokens`, in `/admin/logs` and the usage export for chargeback), and `gateway_tokens_total{type=cached_input}` — per-key too, when `metrics.per_key` is on.

```yaml
pricing:
  claude-sonnet-4-5: {input: 3.0, output: 15.0, cached_input: 0.30}
```

**Cost-aware routing.** `routing.cost_aware: true` ranks same-priority deployments by their **upstream model's input price** (the pricing table, the same longest-prefix lookup billing uses): cheapest first, so "model-a at $5/M and model-b at $1/M both serve `test-model`" stops being a coin flip. The rules, in order:

- **Priority still rules across tiers** — cost never outranks a deliberately lower-priority deployment.
- **Within a tier, cheapest wins; the price ladder doubles as the failover ladder** (an exhausted cheap deployment falls through to the next cheapest, then the existing retry/breaker machinery).
- **Weight only breaks price ties** — equally-priced deployments load-balance by `weight` as before.
- **Unpriced models sort last** — unknown cost gets no discount; price your deployments if you enable this.
- `/admin/routing` echoes `cost_aware` and each deployment's `{input, output, cached_input}_usd_per_mtok` (from the same lookup), so the ranking is inspectable.

15 tests pin the contract (`tests/test_cost_aware.py`).

## Tenant pricing markup

Resellers and platform teams bill tenants at cost-plus. Set `pricing_markup_pct` on a tenant (20.0 = bill member traffic at 1.2× provider cost) and every member-key request — chat, embeddings, images, audio, speech, streams, tenant admins' own traffic — records the **billed** cost everywhere money is accounted:

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name": "acme", "pricing_markup_pct": 20.0, "max_budget_usd": 100}' \
  http://localhost:8000/admin/tenants
```

| Surface | Records |
| --- | --- |
| `spend_usd` (key + tenant), budget windows, budget alerts | billed |
| `X-Gateway-Cost-Usd` response header | billed |
| `RequestLog.cost_usd` → `/admin/logs`, capture export | billed |
| `/admin/usage/export` (chargeback) + `/admin/stats` | billed |
| `gateway_key_cost_usd_total{key}` (per-key metrics) | billed |
| **`gateway_cost_usd_total{model}`** | **actual** — platform economics stay visible beside tenant economics |
| `gateway_markup_usd_total{tenant}` | the markup revenue itself (billed − actual) |

The rules, so the accounting is never surprising:

- **Snapshot at auth time**, like every other tenant ceiling: a PATCH applies from the next request, never mid-flight.
- **Budgets enforce the billed cost** — a tenant's ceiling is what they pay, not what you pay.
- **Platform keys bill at actual**, always (no tenant, no markup); `NULL` (never set) and an explicit `0` both mean no markup.
- **Shadow mirrors record actual provider cost** — mirror rows compare deployments, they don't bill anyone.
- Rounding matches the calculator's (8 decimals), so `billed = cost × (1 + pct/100)` reconciles exactly against the log.

10 tests pin the contract (`tests/test_tenant_markup.py`).

## BYOK vault

Bring-your-own-key in one PUT: a tenant stores *their* provider credential
once and every request from that tenant rides **their** provider account
instead of the platform's configured key — chat (stream and not),
embeddings, images, audio, through every retry and fallback leg. The
provider sees the same traffic; the bill lands on the tenant's account;
the platform's quota is untouched.

```bash
# Enable the vault (a Fernet key — env only, never config.yaml):
export GATEWAY_VAULT_MASTER_KEY="$(python -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# Store acme's own OpenAI key (tenant admin or platform admin):
curl -X PUT http://localhost:8000/admin/tenants/acme/provider-keys/openai \
  -H "Authorization: Bearer $TENANT_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-acme...", "label": "acme-prod"}'
# → {"id": "...", "provider": "openai", "key_hint": "…9911", "label": "acme-prod", ...}

# List / delete (metadata only — the key is never returned, not even
# to the caller that just sent it):
curl http://localhost:8000/admin/tenants/acme/provider-keys \
  -H "Authorization: Bearer $TENANT_ADMIN_KEY"
curl -X DELETE http://localhost:8000/admin/tenants/acme/provider-keys/openai \
  -H "Authorization: Bearer $TENANT_ADMIN_KEY"
```

The security posture, in one breath: the raw key exists exactly **twice** —
in the caller's `PUT` body and in the upstream `Authorization` header. At
rest it is Fernet-encrypted (AES128-CBC + HMAC-SHA256, authenticated) under
`GATEWAY_VAULT_MASTER_KEY`, which the process reads from env and never
persists. Every API surface speaks only a *hint* (`…1234`, the last
characters — enough to tell stored keys apart, never enough to use); audit
records carry the hint, never the key; log rows record only **that** BYOK
was used (`byok_provider` = the provider name) plus the
`X-Gateway-Byok-Provider` response header and
`gateway_byok_requests_total{provider}` — the chargeback-compatibility
metric.

Semantics worth knowing:

- **Per (tenant, provider) granularity** — a tenant keys exactly the
  providers they use. Nothing else changes: platform callers, other
  tenants, and keyless callers are byte-for-byte unaffected.
- **Per-candidate failover resolution** — retries and fallbacks resolve
  the vault *per candidate deployment*: the openai leg rides the tenant's
  stored key, an azure fallback leg (unkeyed) rides the platform's azure
  key. The tenant's key never leaks onto a leg it doesn't belong to.
- **`vault.strict` — the stance** — with `strict: true`, tenant traffic
  may **only** ride credentials the tenant stored: deployments whose
  provider the tenant has *not* keyed are removed from their routing
  ladder entirely, so retries and fallbacks never cross to an unkeyed
  provider — and a model whose every deployment is unkeyed refuses
  403 `byok_key_required`, naming the sorted provider list, the model
  and the exact `PUT` path that fixes it. Platform callers (no tenant),
  platform admins and the platform's own machinery (shadow mirrors,
  canary probes) are untouched; the streaming and realtime ladders
  narrow identically (a realtime refusal is the in-band error event +
  close 1008, the session-shaped envelope). The stance reads live off
  the config per request — hot-reloadable — while `backend`/`ttl_seconds`
  report `restart_required` (they build the service). **The footgun,
  made loud**: strict with *no* master key means no tenant can key
  anything, so tenant traffic refuses rather than silently spending
  the platform's keys — set the master key before turning strict on.
- **Fail closed** — an entry that exists but cannot be decrypted (the
  master key was rotated away without re-storing) fails the request with
  500 `byok_key_unreadable` instead of silently spending the platform's
  key. A tenant that deliberately brought their own key must never be
  silently re-billed to yours. Platform callers are unaffected (the
  corruption is scoped to the tenant + provider that owns the entry).
- **Rotation** — `GATEWAY_VAULT_MASTER_KEY` accepts a comma-separated
  list: the **first** key encrypts new entries, **all** decrypt
  (MultiFernet). Rotate by prepending the new key, restarting,
  re-PUTting entries at leisure, then dropping the old key.
- **Hot path** — decrypted entries are cached in process memory for
  `vault.ttl_seconds` (30 by default), so serving adds a dict read, not
  a DB round trip. Admin writes invalidate the local cache immediately;
  with `vault.backend: redis` they also **publish** the invalidation on
  `{redis.prefix}:vault-invalidation` and every replica's listener drops
  the named entry — and its *negative twin* (the cached "no key
  stored" answer that would otherwise keep routing the platform's key
  for up to the TTL after another replica stored one) — the moment the
  message lands. Both directions are best-effort: a failed publish or a
  malformed message never fails the admin write, Redis outages back the
  listener off 5s, and the TTL remains the backstop. `memory` (default)
  keeps the single-process shape: other replicas converge within the
  TTL.
- **Authz** — tenant admins manage *their own* tenant's credentials only
  (a cross-tenant id is a 404, the same anti-leak semantics as key
  management); platform admins manage any tenant's.
- **Platform machinery rides platform keys** — shadow mirrors, canary
  probes and the semantic-cache embedder authenticate with the
  platform's configured credentials (they are the gateway's own
  traffic, not the caller's).
- **Off by default** — no master key, no vault: the provider-key
  endpoints answer 400 `vault_unavailable` naming exactly what is
  missing (never a silent plaintext store), and every caller is served
  exactly as before. The vault requires
  `pip install 'llm-gateway[vault]'` (cryptography).

The dashboard's **Vault tab** (between Tenants and Prompts) manages all
of this without curl: `GET /admin/vault` reports the honest state —
`enabled` or the `unavailable_reason` naming the env var to set, the
`strict` stance, `invalidation_backend` and `ttl_seconds`, plus
`admin_context` — so a **platform admin** picks any tenant from a select
and a **tenant admin** is pinned to their own (the only one the
provider-key endpoints let them touch). The credential rows show
provider, label, hint and updated-at; the key input is `type=password`
and cleared after every store, and the dashboard only ever sees hints.

11 tests pin the tier-31 contract (`tests/test_byok_vault.py`); 18 more
pin the tier-34 hardening — the strict stance (refusals, ladder
narrowing, the hot-reload contrast, the no-master-key footgun,
unreadable fail-closed, streaming + realtime), the `/admin/vault`
status surface, and the Redis invalidation (publish-on-write, two
replica convergence on both the store and delete directions, malformed
and failed-publish best-effort) — `tests/test_byok_hardening.py`.

## Runaway-agent protection

Agents get stuck: a retry storm without backoff, a tool-call loop that regenerates forever, a temperature-zero do/while. Each request is individually legal, so RPM/TPM never trips — what identifies the runaway is the **fingerprint** (the response cache's normalized request identity) repeated `identical_request_threshold` times inside `identical_request_window_seconds` (default: 10 in 60s — above any sane retry pattern; raise it if you run legitimate identical-request fan-out, e.g. benchmarks, through the gateway).

On trip, the **key** enters a cooldown jail: every request from it answers `429 rate_limit_exceeded` + `Retry-After` until the cooldown expires — including requests the response cache would serve free and idempotency replays. Nothing a stuck agent sends is useful.

The semantics that keep it safe:

- **Escalating** — a still-looping agent re-trips after the cooldown, and the sentence doubles per offense (`cooldown_multiplier`, capped at `cooldown_max_seconds`); a quiet `reset_after_seconds` decays the offense count.
- **Punishment, not a grudge** — loop windows are cleared on trip: after the cooldown a single investigation retry passes while a real loop re-trips on fresh hits.
- **Cross-surface** — the fingerprint is the normalized request, stable across OpenAI chat / `/v1/responses` / native Anthropic & Gemini, and streaming doesn't change it: an agent looping across surfaces or alternating stream flags is still one loop.
- **Exemptions** — admin/env-key traffic (no key id) is exempt, like every key-layer ceiling. Chat-family only: embeddings/images/audio are single-shot surfaces, not loops.
- **Hot-reloadable** — thresholds read the live config per call; jail state survives a reload (same trade the router makes with breakers). In-process detection state (like the idempotency in-flight map): a multi-replica fleet detects at per-replica rate — bounded, just slower to trip.

Observability: `runaway.trip` observer event (`key_name`, `model`, `count`, `threshold`, `cooldown_seconds`, `offenses` — alarm or revoke from your orchestration layer), `gateway_runaway_trips_total{key}` counter, and the admin surface — `GET /admin/runaway` (jail snapshot + config echo, key names joined) and `POST /admin/runaway/{key_id}/release` (the escape hatch: clears jail, offense history and loops; audited as `runaway.released`; a released key that loops again starts at the first-offense cooldown).

```yaml
runaway:
  enabled: true
  identical_request_threshold: 10   # identical requests inside the window = a stuck loop
  identical_request_window_seconds: 60
  cooldown_seconds: 30              # first offense; repeats escalate
  cooldown_multiplier: 2.0          # 30s -> 60s -> 120s ...
  cooldown_max_seconds: 3600        # 1h ceiling
  reset_after_seconds: 600         # quiet period that clears the offense count
```

14 tests pin the contract (`tests/test_runaway.py`).

## Concurrency admission control

RPM/TPM bound the *rate* of requests; a concurrency cap bounds how many may be **in-flight against the upstream at once** — the number that actually protects a provider quota, a rate-limited deployment fleet, or a small pool of expensive models. Caps live on the rows, snapshotted at auth time like every other ceiling:

- **Per key** — `max_concurrency` on a virtual key (`POST/PATCH /admin/keys`, admin-UI key dialog; `null` = unlimited).
- **Per tenant, pooled** — `max_concurrency` on a tenant (`POST/PATCH /admin/tenants`): the ceiling across *every* member key's upstream requests; a member must pass both its own cap and the tenant's (the tenant can only narrow).

The slot wraps the **upstream call**, acquired after the cache miss:

- Cache hits and idempotent replays never touch the upstream, so they never queue — a capped key's repeat traffic stays instant.
- A streamed response holds its slot until its body completes — a slow reader occupies upstream capacity for exactly as long as it reads.
- Every error path releases (retry exhaustion, guardrail blocks happen before acquisition, client disconnects) — the release sits where the in-flight gauge decrements, so a leaked slot is structurally impossible.

When every slot is taken, a request does not fail — it **queues**, and admission is priority-ordered QoS:

- `priority` on the key (default 0, range −1000…1000): when waiters contend for a cap, the highest priority is admitted first, FIFO within a class. Priority only matters under contention — and since it's a *key* attribute, it matters exactly where contention crosses keys (typically the pooled tenant cap).
- A waiter whose own key is saturated never blocks waiters on other keys: the queue drains in priority order and *skips* (not stops) at a waiter that does not fit.
- A queue wait past `concurrency.wait_seconds` answers `429 rate_limit_exceeded` + `Retry-After` (`0` = fail fast when all slots are taken) and fires a `concurrency.timeout` observer event (key, model, cap, wait and actual waited seconds — alarm from your orchestration layer).

Observability: `gateway_concurrency_inflight{key}` / `gateway_concurrency_queued{key}` gauges, `gateway_concurrency_rejected_total{key}` counter, and `GET /admin/concurrency` (platform admin): live in-flight/queued counts per key and tenant with name joins, plus the config echo.

Honest boundaries: chat-family surfaces only (embeddings/images/audio run through `_serve_simple` and never acquire a slot — same boundary as runaway detection); callers with neither a key cap nor a tenant cap skip admission entirely (no queueing, no metrics); admin/env-key traffic is exempt like every key-layer ceiling; batch lines go through the chat pipeline and are capped like any other request. State is in-process (like the idempotency in-flight map and runaway detection): a multi-replica fleet enforces its per-replica share — run N replicas behind a load balancer with cap/N, or accept the approximation.

```yaml
concurrency:
  enabled: true
  wait_seconds: 10        # queue wait before 429; 0 = fail fast
```

15 tests pin the contract (`tests/test_concurrency.py`).

## Data-residency routing

Some traffic must never leave some jurisdictions — a German customer's prompts on EU-deployed models only, a healthcare tenant's data in-region. That is a *routing* constraint, not a guardrail: the decision is about **where** the request runs, and it must hold even when things fail. This tier makes it explicit, operator-asserted, and fail closed.

Two sides, both in your control:

- **Deployments claim** — `residency` on each deployment in `config.yaml`: a lowercase slug (`eu`, `us`, `eu-west`) asserting where that deployment processes data. The operator asserts it; the gateway never guesses. **None = NO CLAIM** — an untagged deployment satisfies no requirement, because it can never *prove* compliance.
- **Callers require** — `required_residencies` on a virtual key and/or its tenant (admin API, admin UI): the tags that may serve that caller's traffic. A tenant's requirement binds every member key; the effective set is the **intersection** (key ∩ tenant — the tenant can only narrow), and an empty intersection means nothing can serve: fail closed, 404.

```yaml
models:
  - name: gpt-4o
    deployments:
      - name: azure/eu-gpt-4o
        provider: azure
        model: gpt-4o-prod
        residency: eu          # operator-asserted: processes data in the EU
      - name: openai/gpt-4o
        provider: openai
        model: gpt-4o
        priority: 200
        residency: us          # a fallback for us-restricted and unrestricted callers
```

The filter lives inside `Router.resolve` — **before** the priority ladder, retries and fallbacks. That placement is the whole feature:

- A constrained request can never escape to a non-compliant deployment mid-failover: the us fallback is not "tried last", it is *not on the ladder*.
- Failures still fail over — **within** the compliant class. Residency narrows the ladder; it does not disable it.
- No compliant deployment → `404 residency_not_satisfied`, naming the model and the required tags — distinguishable from a missing model. Nothing is ever consulted upstream.
- Every modality routes through the same `resolve`: chat (all inbound formats, stream and not), embeddings, images, audio. And **shadow mirrors too** — a mirror is a copy of the caller's data, so it may only land on compliant deployments; non-compliant shadows silently receive nothing (the mirror is best-effort by design).

Caches respect the boundary without losing reuse:

- The requirement salts the chat **response-policy token** and the **embeddings cache key** — residency classes never share entries, in either direction, because an entry produced under one requirement may have been generated on a deployment another class may not use. Only salted in when a requirement exists: unrestricted callers keep their exact pre-tier-28 keys, so existing cache entries stay valid.
- The **semantic cache degrades, never violates**: a restricted caller's prompt is only embedded through the router's residency filter, and a model with no compliant embedder deployment is treated as a plain miss — the request still serves, it just loses semantic hits.

Semantics details: tags are matched **exactly** after normalization (lowercase slugs `^[a-z0-9][a-z0-9_-]*$`; `EU` and `eu` are the same tag, `not a slug!` is a 400), PATCH `[]` and `null` both clear a requirement, admin/env-key traffic carries no key so it is exempt (like every key-layer ceiling), and `GET /admin/routing` shows every deployment's residency claim — `null` visible as "no claim".

Honest boundary: the gateway enforces the *tags you assert*; it cannot verify a provider's actual physical location. The assertion is yours — the enforcement is exact.

14 tests pin the contract (`tests/test_residency.py`).

## Architecture

For a deep dive — module map, request lifecycle sequence diagrams, extension frameworks, coupling rules, and an honest modularity assessment — see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

```
gateway/
├── main.py               # FastAPI app factory, lifespan, error handlers
├── config.py             # env settings + YAML config (providers, models, guardrails, ...)
├── schemas.py            # OpenAI-compatible pydantic models
├── models.py / db.py      # SQLAlchemy async ORM: virtual keys, request logs
├── api/                  # routes: chat, models, admin; auth chain + scoping deps
├── core/
│   ├── pipeline.py       # request lifecycle orchestration
│   ├── router.py        # deployment selection, retries, fallbacks
│   ├── breaker.py       # per-deployment circuit breakers
│   ├── ratelimit.py     # sliding-window RPM/TPM
│   ├── redis_backends.py # Redis rate-limit + cache backends, shared breaker state
│   ├── cache.py         # exact-match response cache
│   ├── semantic_cache.py # similarity (semantic) response cache
│   ├── cost.py          # pricing + cost calculation
│   ├── auth.py          # pluggable auth chain: admin/virtual keys, JWT, custom
│   ├── prompts.py       # managed system prompts + {{variable}} templating
│   ├── prompt_library.py # versioned prompt library: CRUD + cached resolution,
│                        # active/candidate model, A/B mirroring support
│   ├── audit.py         # audit webhooks for admin actions
│   ├── alerts.py        # budget-threshold webhook alerts
│   └── logging_svc.py   # request logs + spend accounting (opt-in background queue)
├── guardrails/
│   ├── base.py          # Guardrail ABC + result types (redirect / field rewrites /
│   │                    # mid-stream rewriting hooks)
│   ├── manager.py       # phase executor + registry + custom loading
│   └── builtins/        # pii_redaction, prompt_injection, keyword_blocklist,
│                        # output_moderation, response_schema, request_limits,
│                        # context_window
├── providers/           # openai (passthrough + native Responses), azure,
│                        # anthropic, gemini adapters
└── observability/       # Prometheus metrics + OpenTelemetry tracing setup
```

### Request lifecycle

1. **Auth & model resolution** — the auth chain (`admin_key` / `virtual_key` / `jwt` / custom steps; virtual keys are hashed at rest, JWT callers adopt the named key's policy), then token scoping (`allowed_endpoints` / `allowed_ips`); `model_aliases` / `default_model` normalize the requested name to a public model (allowlist, cache identity, guardrail scopes and the echoed response see the resolved name; the log row keeps the original); model allowlist, expiry, active checks. Then the **prompt policy**: the managed system prompt (per-key, else `prompts.system_prompt`) is prepended when the request carries no system message of its own, and `{{variables}}` resolve in message text — before guardrails and cache lookup, so checks and cache identity see the final prompt
2. **Rate limit** — sliding-window RPM/TPM per key
3. **Budget** — pre-flight spend check against the key's USD budget
4. **Guardrails (request phase)** — may redact/modify the prompt, rewrite request fields, **redirect the model** (reject-and-redirect; the key's allowlist is re-checked, fail closed) or block
5. **Cache** — exact-match lookup (post-redaction); hits replay, including streams
6. **Routing** — weighted pick among same-priority deployments; retryable errors retried with backoff, then failover to the next priority tier
7. **Provider call** — request translated to the provider's native format (messages, tools, images, sampling params)
8. **Guardrails (response/stream phase)** — may block the output (surfaced as `finish_reason: "content_filter"`, matching OpenAI semantics) or **rewrite streamed text as it streams** (held-back text flushes before the finish chunk, so masked PII never reaches the client)
9. **Accounting** — usage, cost, spend, request log, metrics
10. **Response** — OpenAI schema, with `X-Gateway-*` headers

## Extending with custom guardrails

Subclass `Guardrail`, implement the phases you care about, and register it in `config.yaml`:

```python
# my_guardrails/ban_competitors.py
from gateway.guardrails.base import Guardrail, GuardrailResult

class CompetitorBanGuardrail(Guardrail):
    name = "competitor_ban"
    description = "Blocks prompts that mention competitor products."

    def __init__(self, params=None):
        super().__init__(params)
        self.competitors = self.params.get("competitors", [])

    async def check_request(self, request, ctx):
        text = " ".join(m.content for m in request.messages if isinstance(m.content, str))
        for c in self.competitors:
            if c.lower() in text.lower():
                return GuardrailResult.block(f"competitor mention: {c}")
        return None
```

```yaml
guardrails:
  - name: competitor_ban
    enabled: true
    class: "my_guardrails.ban_competitors:CompetitorBanGuardrail"
    params:
      competitors: ["AcmeLLM", "BrandAI"]
```

Available phases: `check_request` (incoming, may modify/block — modifications can carry messages, the model, and arbitrary request fields), `check_response` (outgoing non-streaming, may modify/block), `check_stream_delta` (outgoing streaming; block aborts the stream with `content_filter`), `rewrite_stream_text` (outgoing streaming; *rewrites* the text as it streams). See `gateway/guardrails/base.py`.

#### Reject-and-redirect

A request-phase guardrail does not have to block policy-violating traffic — it can reroute it. `GuardrailResult.redirect(model, reason)` serves the request on another public model instead (a "this content is too expensive for the flagship model" policy):

```python
async def check_request(self, request, ctx):
    if estimated_cost(request) > self.threshold:
        return GuardrailResult.redirect("gpt-4o-mini", "long context routed to the small model")
    return None
```

The pipeline re-checks the key's model allowlist after the guardrail phase — a redirect can never bypass permissions (fail closed: the request is refused, not silently rerouted). Everything downstream (routing, logs, metrics, cache identity) sees the final model, while `request_body` in the log keeps what the client originally asked for. Two built-ins expose the same pattern from config: `request_limits` (`on_exceed: redirect` + `redirect_model` sends oversized requests to a cheaper model) and `context_window` (over-window requests to a bigger model, or an OpenAI-style 400 before quota is spent — see [Context-window management](#context-window-management)).

#### Mid-stream rewriting

`check_stream_delta` can only block — by the time it fires, the flagged text has already reached the client. A guardrail that sets `supports_stream_rewrite = True` and implements

```python
async def rewrite_stream_text(self, text: str, *, final: bool = False) -> tuple[str, str]:
    """text = everything not yet emitted (pending + the new delta).
    Returns (emit_now, keep_pending): hold back only what might still
    change. final=True means no more text arrives — return everything."""
```

rewrites the stream *as it flows*: each delta passes through the chain of rewriters (each sees the previous one's output), held-back text flushes right before the stream's finish chunk and at the end of the stream, and the response body / cache entry store what the client actually saw. Block-style checks (`check_stream_delta`) still see the **source** text from the provider — moderation judges what the model said, not the rewrite. The built-in `pii_redaction` implements this with `mask_in_stream: true`: it holds back the last `stream_reserve_chars` (default 100) of each delta, because a match split across provider chunks (an email arriving in pieces) must never leak half-redacted.

## Extending with custom authenticators

The auth chain loads custom steps the same way. Subclass `Authenticator`, and list it in `auth.chain`:

```python
# my_auth/static_token.py
from gateway.core.auth import Authenticator, auth_context_from_key, get_key_by_name
from gateway.core.context import AuthContext

class StaticTokenAuthenticator(Authenticator):
    name = "static_token"  # shows up on log rows' auth_method + metrics

    def __init__(self, config):  # receives the whole auth config
        pass

    async def authenticate(self, credential: str) -> AuthContext | None:
        if not credential.startswith("tok-"):
            return None  # not ours — let the next step try
        vkey = await get_key_by_name(credential.removeprefix("tok-"))
        if vkey is None:
            return None
        # the virtual key row supplies all policy (budgets, limits, scopes)
        return auth_context_from_key(vkey, auth_method=self.name)
```

```yaml
auth:
  chain: [admin_key, "my_auth.static_token:StaticTokenAuthenticator"]
```

Return an `AuthContext` to accept (chain stops), `None` to pass on, or raise `AuthError` to reject the credential outright (401). See `gateway/core/auth.py`.

### Observe mode

Before enforcing a new guardrail on live traffic, run it in **observe mode** to measure its would-be verdicts (false-positive rate) without touching any request:

```yaml
guardrails:
  - {name: prompt_injection, enabled: true, mode: observe, params: {threshold: 3}}
```

Verdicts that *would have* blocked or modified are recorded in `/admin/logs` (the `mode` field distinguishes them), counted on `gateway_guardrail_observed_total`, and emitted as `gateway.guardrail.observed` span events — but the request flows on untouched, and later guardrails still run. `gateway_guardrail_blocks_total` stays enforce-only, so existing alerts keep their meaning.

### Guardrail scoping

Two mechanisms decide *which requests* a guardrail applies to, and they compose:

1. **Config scope** — `guardrails[].scope` with fnmatch globs over key names, public model names, and inbound routes. `exclude_*` wins over the matching include list; a request must match every present field:

```yaml
guardrails:
  - name: pii_redaction
    scope:
      keys: ["mobile-*", "web-*"]       # only run for these key names
      exclude_models: ["*-mini"]        # ...except on these models
      routes: ["chat"]                  # ...and only on /v1/chat/completions
```

Route values name the inbound API surface: `chat` (`/v1/chat/completions`), `responses` (`/v1/responses`), `messages` (native Anthropic), `gemini` (native Gemini) — per-route policy like "strict checks on the OpenAI chat surface, looser on the native formats" needs no separate guardrail instances.

2. **Per-key runtime override** — `disabled_guardrails` on a virtual key (set via `POST/PATCH /admin/keys` or the admin UI, no restart needed). A key's disabled list always wins over config scope.

Out-of-scope guardrails are skipped entirely — no verdict, no log entry, no metric — as if they were not configured. Scopes are matched against the **resolved** public model name (after `model_aliases` — and after any guardrail redirect), and the admin key counts as key name `"admin"`. `/admin/guardrails` and the admin UI show each guardrail's scope.

### Presidio PII detection (optional extra)

The `presidio_pii` guardrail replaces the regex heuristics with [Microsoft Presidio](https://github.com/microsoft/presidio) for production-grade detection:

```yaml
guardrails:
  - name: presidio_pii
    enabled: true
    params:
      entities: [EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD, US_SSN, PERSON, LOCATION]  # default: curated set
      block_entities: [CREDIT_CARD]   # these block instead of redact
      mask_in_response: true          # also redact the model's output
      score_threshold: 0.3
      use_nlp: true                   # false → pattern-only, no model load
```

Install the optional extra first: `uv sync --extra guardrails`. By default the full NLP engine runs (context-aware scoring, NER entities like `PERSON`/`LOCATION`; loads a ~425 MB spacy model per process). `use_nlp: false` builds a pattern-only engine — no model, lower accuracy, NER entities unavailable. Analysis runs in a worker thread; detected spans are replaced with `<ENTITY_TYPE>` placeholders (configurable via `placeholder`). Like the regex builtin it covers the request and (optionally) response phases — not streaming output.

## Configuration reference (`config.yaml`)

```yaml
providers:            # upstream endpoints + API key env var names
  openai:  {api_base: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY}
  # Azure OpenAI: deployment-addressed URLs, `api-key` header. `model` on a
  # deployment below is the Azure *deployment name*; api-version defaults
  # to a recent stable value and can be pinned per provider.
  azure:   {api_base: https://myresource.openai.azure.com, api_key_env: AZURE_OPENAI_API_KEY, api_version: "2024-10-21"}

models:               # public name → deployments (priority: lower = preferred)
  - name: gpt-4o
    context_window: 128000  # optional: enables the context_window guardrail
    deployments:
      - {name: openai/gpt-4o, provider: openai, model: gpt-4o}
      - {name: anthropic/fallback, provider: anthropic, model: claude-sonnet-4-5, priority: 200}
      # Shadow deployment: never serves clients; a mirrored copy of
      # shadow_fraction (default all) of successful requests is sent to it
      # for comparison (own log rows + metrics; the client is unaffected).
      # Canary rollouts need no separate flag: raise `weight` gradually
      # within a priority tier to shift real traffic instead.
      - {name: azure/gpt-4o-shadow, provider: azure, model: gpt-4o-prod,
         shadow: true, shadow_fraction: 0.1}
      # Native Responses southbound: requests that arrived via
      # POST /v1/responses are forwarded to the provider's /responses
      # endpoint in their original shape instead of the chat conversion
      # (see the design notes). OpenAI + Azure deployments only.
      # - {name: openai/gpt-4o-native, provider: openai, model: gpt-4o,
      #    native_responses: true}
  # Embeddings models are declared the same way (used by POST /v1/embeddings).
  # Any OpenAI-compatible base works; Gemini is translated to batchEmbedContents.
  # (Anthropic has no embeddings API — its deployments return a clear 501.)
  - name: text-embedding-3-small
    deployments:
      - {name: openai/emb-small, provider: openai, model: text-embedding-3-small}
  # Image and audio models are declared exactly like chat models and get the
  # same routing/retries/failover/breakers/budgets/rate limits/logs. Images:
  # generations/edits/variations — OpenAI/Azure pass through, Gemini
  # translates generation + edits to generateContent. Audio
  # (transcription/translation/TTS): OpenAI/Azure pass through; Gemini
  # speaks TTS natively (gemini-2.5-*-preview-tts, voice mapping, WAV out).
  - name: gpt-image-1
    deployments:
      - {name: openai/gpt-image-1, provider: openai, model: gpt-image-1}
  - name: whisper-1
    deployments:
      - {name: openai/whisper-1, provider: openai, model: whisper-1}
  - name: tts-1
    deployments:
      - {name: openai/tts-1, provider: openai, model: tts-1}

guardrails:           # built-ins by name, custom via `class:' — mode: observe logs without enforcing
  - {name: pii_redaction, enabled: true, params: {mask_in_response: true, mask_in_stream: true}}
  - {name: prompt_injection, enabled: true, params: {threshold: 3},
     scope: {exclude_keys: [agent-*]}}   # run everywhere except for agent-* keys
  - {name: request_limits, enabled: false,
     params: {max_messages: 100, max_chars: 200000,
              on_exceed: redirect, redirect_model: gpt-4o-mini}}  # oversized -> cheaper model, not blocked
  - {name: context_window, enabled: false,
     params: {reserve_output_tokens: 1024, on_exceed: block,
              redirect_model: gpt-4o}}  # over models[].context_window -> 400 pre-flight, or bigger model
  # scope fields: keys / exclude_keys / models / exclude_models / routes /
  # exclude_routes (fnmatch globs; exclude wins; all present fields must
  # match; routes = chat | responses | messages | gemini). See "Guardrail
  # scoping" above.
  # pii_redaction params: mask_in_stream holds back the last
  # stream_reserve_chars (default 100) of each delta so matches split
  # across provider chunks never leak half-redacted.

# Map client-requested names to your public model names, so apps with
# hardcoded provider names work unchanged. Real model names always win
# over aliases; aliases appear in GET /v1/models.
model_aliases:
  gpt-4o: gpt-4o            # requested -> public name (a configured model wins)
  claude-3-5-sonnet-20241022: claude-sonnet
# default_model: gpt-4o     # opt-in catch-all for unknown names (use with
                            # care — typos silently route here too)

# Gateway-managed prompt policy. system_prompt is prepended to requests
# that carry no system message of their own (a key's system_prompt
# overrides this; a client-supplied system message always wins over
# both). variables resolve {{placeholders}} in message text and in the
# managed prompt — a key's prompt_variables merge over these (key wins);
# unknown placeholders are left untouched.
prompts:
  system_prompt: "You are a helpful assistant. Be concise."
  variables:
    company: "Acme"
    tone: "formal"
  # How long a key's resolved system_prompt_ref (named library prompt)
  # is cached in-process. Writes through this process's admin API
  # invalidate immediately; the TTL is the cross-replica backstop.
  library_cache_seconds: 30

logging:
  max_body_chars: 20000
  #store_bodies: false      # PII-safe mode: don't persist request/response
                            # bodies (columns stay NULL; metadata still is)
  #retention_days: 30       # delete request log rows older than this
  #retention_interval_seconds: 3600   # prune cadence (startup, then this)
  #background_write: true   # queue log writes in the background instead of
                            # awaiting them in the response path (hot-path
                            # optimization; full queue drops rows, counted
                            # on gateway_logs_dropped_total)
  #queue_size: 1000         # bound of that background queue
  # Wire capture on log rows (allowlists — allow, never block, so new
  # credential headers can't leak by default; trailing "*" matches
  # prefixes). Client list read per request (reload applies live);
  # upstream list bound into the provider client's hooks (restart).
  #client_header_allowlist: ["user-agent", "x-request-id"]
  #upstream_header_allowlist: ["x-request-id", "retry-after", "x-ratelimit-*"]

#metrics:
#  per_key: true            # per-key request/token/cost series (opt-in —
                            # key names are unbounded label cardinality)
#  per_key_max_series: 256  # distinct keys before labels collapse into __overflow__

cache:   {enabled: true, ttl_seconds: 300, max_entries: 256, backend: memory}
# Semantic (approximate-match) cache: after an exact miss, embed the request
# and serve the best same-model cached response with >= threshold cosine
# similarity. Costs one embeddings call per uncached request.
#semantic_cache:
#  enabled: true
#  model: text-embedding-3-small   # embeddings model (must have a deployment)
#  threshold: 0.95                 # cosine similarity required for a hit
#  ttl_seconds: 300
#  max_entries: 256
# Multi-worker/multi-replica: share rate-limit windows, cache entries and
# circuit-breaker state via Redis (url also overridable with the REDIS_URL
# env var below)
#redis:  {url: redis://localhost:6379/0, url_env: REDIS_URL, prefix: llm-gateway}
#rate_limit: {backend: redis}
#cache:  {backend: redis}
#routing:
#  breaker: {backend: redis}   # one replica's trip opens the circuit fleet-wide
routing:
  timeout_seconds: 300
  max_retries: 2
  retry_backoff_base: 0.5
  cost_aware: false                # same-priority tier ranked by input price
  breaker: {enabled: true, failure_threshold: 3, cooldown_seconds: 30}
alerts:                # webhook when a key's spend crosses a % of its max_budget_usd
  budget_thresholds: [50, 80, 90, 100]        # each level fires once per key
  expiry_days: [14, 7, 1, 0]                  # key-expiry warnings: day levels; 0 = the expired page
  #expiry_interval_seconds: 3600              # expiry sweep cadence (default: hourly)
  webhook_url: https://hooks.slack.com/services/T000/B000/XXXX   # or url_env: ALERT_WEBHOOK_URL
  timeout_seconds: 5                          # per POST attempt
  max_attempts: 2                             # 1 retry (0.5s backoff)
audit:                 # webhook for security-relevant admin actions (key CRUD, breaker reset)
  webhook_url: https://siem.example.com/collect   # or url_env: AUDIT_WEBHOOK_URL
  timeout_seconds: 5
  max_attempts: 2
vault:                 # BYOK vault: tenants store their OWN provider credentials (encrypted).
                       # ENABLED by GATEWAY_VAULT_MASTER_KEY (env only — it encrypts secrets,
                       # so it is not itself committed). Requires pip install 'llm-gateway[vault]'.
  backend: memory      # cache-invalidation fan-out across replicas: memory (per process,
                       # others converge within the TTL) | redis (admin writes also publish on
                       # {redis.prefix}:vault-invalidation and every replica's listener drops
                       # its cached entry + negative twin immediately; TTL stays the backstop)
  strict: false         # platform-economics stance: tenant traffic may ONLY ride credentials
                       # the tenant stored — unkeyed deployments are removed from a strict
                       # tenant's routing LADDER (retries/fallbacks never cross to an unkeyed
                       # provider), and a model with no keyed deployment refuses 403
                       # byok_key_required naming the providers to key. Platform callers,
                       # platform admins and the platform's own machinery are untouched.
                       # FOOTGUN: strict with NO master key means tenant traffic cannot run.
  ttl_seconds: 30      # per-(tenant, provider) cache of decrypted keys in process memory
auth:                  # the authentication chain, in order (default: admin_key + virtual_key)
  chain: [admin_key, virtual_key]
  # jwt:                       # add "jwt" to the chain to accept OAuth/JWT bearer tokens
  #   secret: shared-hs256-secret          # or jwks_url: https://idp/.well-known/jwks.json
  #   key_claim: sub                      # claim naming the virtual key (its policy applies)
  #   issuer: https://idp.example.com     # enforced when set (exp always; audience too)
  #   audience: llm-gateway
  #   leeway_seconds: 30                  # clock-skew tolerance
  #   # jwks_cache_seconds: 300            # key rotation refreshes early on unknown kid
# Multipart upload policy: the gateway buffers multipart uploads (audio
# files, edit/variation images + masks) to route and retry them, so
# max_upload_mb caps their total size (413 beyond it; matches OpenAI's own
# 25 MB limit). Hot-reloadable.
uploads:
  max_upload_mb: 25
# Batch API (POST /v1/files + /v1/batches): a background worker executes
# wave_size lines of the oldest runnable batch every poll_interval_seconds
# — each line through the full pipeline (guardrails, cache, spend, logs).
# enabled: false 404s the surfaces and stops the worker. Hot-reloadable.
batch:
  enabled: true
  max_file_bytes: 20971520   # 20 MB input file cap (matches OpenAI)
  max_requests: 10000        # input lines per batch
  wave_size: 16
  poll_interval_seconds: 2.0
# MCP proxy (POST /v1/mcp/{server}): transparent Streamable-HTTP proxy
# to operator-configured MCP servers. Upstream auth lives HERE (the
# caller's gateway key never travels upstream); `headers_env` entries
# are env var names resolved at call time (win over literals).
# Hot-reloadable; session_ttl sweeps idle gateway sessions (the client
# re-initializes, per the transport spec).
mcp:
  enabled: true
  session_ttl_seconds: 3600
  servers:
    - name: weather
      url: https://mcp.internal.example/mcp
      headers: {Authorization: "Bearer dev-token"}   # literal (dev/tests)
      headers_env: {Authorization: MCP_WEATHER_TOKEN} # env names (production)
      timeout_seconds: 30                            # optional per-call timeout
# Lifecycle events: subscribe handlers to the request-stage boundaries
# (request.received, response.completed, stream.*, auth.rejected, key.*,
# budget.threshold, batch.* ...). kind: observer = fire-and-forget on a
# background queue (cannot affect requests); interceptor = in-band, may
# abort the request (GatewayError) — only request.received and
# response.completed, listed explicitly. With no handlers, emission is
# a no-op. Hot-reloadable (queue_size applies at restart).
events:
  enabled: true
  queue_size: 10000
  handlers: []
pricing:
  gpt-4o: {input: 2.5, output: 10.0}      # USD per 1M tokens
  #  cached_input: 1.25                    # per-1M price for prompt-cache reads;
                                          # defaults to 50% of input when omitted
  text-embedding-3-small: {input: 0.02}   # embeddings: input-only pricing
  # Non-token modalities use unit-price keys (same longest-prefix match):
  gpt-image-1: {image_usd: 0.04}               # per generated image
  whisper-1: {audio_usd_per_minute: 0.006}     # per transcribed/translated minute
  tts-1: {speech_usd_per_1m_chars: 15.0}       # per 1M input characters
```

Because the OpenAI adapter is a passthrough, you can point `providers.openai.api_base` at **any** OpenAI-compatible service (vLLM, Ollama, Groq, Together, ...) — the gateway features (keys, guardrails, caching, logging) apply to all of them.

## Observability

**Metrics (Prometheus, always on)** — exposed at `/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `gateway_requests_total` | counter | provider, model, status (`success` / `error` / `blocked` / `cache_hit` / `retry`) |
| `gateway_request_latency_seconds` | histogram | provider |
| `gateway_stream_ttft_seconds` | histogram | model — time-to-first-chunk for streams |
| `gateway_requests_inflight` | gauge | currently processing requests (streams count until the body completes) |
| `gateway_realtime_sessions` | gauge | provider, model — currently open realtime (WebSocket) sessions; a session holds its upstream connection for its whole life |
| `gateway_mcp_sessions` | gauge | server — currently open MCP proxy sessions (gateway-issued `Mcp-Session-Id`s; DELETE, lazy upstream-404 drops and the TTL sweep decrement it) |
| `gateway_mcp_requests_total` | counter | server, method, outcome — one per proxied JSON-RPC message (method e.g. `initialize`/`tools/list`/`tools/call`, or `notification`/`response`/`unknown`; outcome success/error/blocked/relayed) |
| `gateway_circuit_state` | gauge | deployment, provider — circuit breaker state (0=closed, 1=half_open, 2=open) |
| `gateway_circuit_transitions_total` | counter | deployment, provider, state — breaker trips / recoveries |
| `gateway_tokens_total` | counter | type (prompt/completion) |
| `gateway_cost_usd_total` | counter | model — actual upstream cost (tenant markups never touch this series) |
| `gateway_markup_usd_total` | counter | tenant — pricing-markup revenue (billed − actual, from a tenant's `pricing_markup_pct`) |
| `gateway_guardrail_blocks_total` | counter | guardrail, phase — enforce mode only |
| `gateway_guardrail_observed_total` | counter | guardrail, phase, action — would-be verdicts in observe mode |
| `gateway_prompt_compression_saved_tokens_total` | counter | model — estimated input tokens removed by enforced `prompt_compression` folds/truncations (observe mode and replay runs never count); the ROI metric — multiply by your input price |
| `gateway_cache_hits_total` | counter | result (hit / semantic / miss) |
| `gateway_alerts_total` | counter | kind, outcome — budget-threshold alerts (sent / failed / skipped-when-no-URL) |
| `gateway_auth_total` | counter | authenticator, result (authenticated / rejected) — per auth-chain step |
| `gateway_audit_events_total` | counter | event, outcome — admin-action audit records (sent / failed / skipped-when-no-URL) |
| `gateway_byok_requests_total` | counter | provider — requests whose upstream call authenticated with the **tenant's** stored credential (BYOK vault) instead of the platform's key; this traffic does not bill the platform's provider accounts |
| `gateway_logs_pruned_total` | counter | request log rows deleted by the retention task (`logging.retention_days`) |
| `gateway_logs_dropped_total` | counter | request log rows dropped because the background write queue (`logging.background_write`) was full |
| `gateway_key_requests_total` | counter | key, status — requests per virtual key (`metrics.per_key`; shadow mirrors excluded) |
| `gateway_key_tokens_total` | counter | key, type — prompt/completion tokens per virtual key (`metrics.per_key`) |
| `gateway_key_cost_usd_total` | counter | key — cost per virtual key (`metrics.per_key`; the *billed* cost when the key's tenant carries a markup) |
| `gateway_key_label_overflow_total` | counter | per-key labels collapsed into `__overflow__` past `metrics.per_key_max_series` distinct keys |
| `gateway_shadow_requests_total` | counter | deployment, outcome (success / error) — requests mirrored to shadow deployments (never affect the client) |
| `gateway_shadow_latency_seconds` | histogram | deployment — latency of mirrored (shadow) upstream calls |
| `gateway_runaway_trips_total` | counter | key — runaway-agent loop detections (each trip jails the key for an escalating cooldown) |
| `gateway_concurrency_inflight` | gauge | key — upstream requests currently holding a concurrency slot (keys with no cap skip admission entirely, so they never appear) |
| `gateway_concurrency_queued` | gauge | key — requests currently queued for a concurrency slot |
| `gateway_concurrency_rejected_total` | counter | key — requests refused after waiting past `concurrency.wait_seconds` (429 + Retry-After) |

**Request logs (always on)** — every request lands in the DB (request/response bodies, guardrail decisions, tokens, cost, latency, cache status; streamed rows additionally carry `ttft_ms` — milliseconds from request start to the first content-bearing chunk off the wire — and the derived decode rate, completion tokens per second after the first token) and is queryable via `GET /admin/logs` and `GET /admin/stats` (which reports TTFT percentiles over streamed rows). TTFT semantics: provider-side (cache-hit replays record `NULL`, same rule as wire capture — nothing went on the wire); the Prometheus `gateway_stream_ttft_seconds` histogram is the client-side counterpart (first chunk delivered, cache hits included). Three compliance knobs in `logging:`: `store_bodies: false` stops persisting request/response bodies (metadata — tokens, cost, latency, status, guardrail verdicts — still is; note `guardrail_actions` details and `error_message` may still quote traffic fragments), `retention_days` makes a background task delete older rows (counted on `gateway_logs_pruned_total`), and `background_write: true` queues the row INSERT to a background consumer instead of awaiting it in the response path — see the design notes below for the trade.

**Wire capture (headers + what actually went to the provider)** — each row also stores, via strict allowlists: the inbound request headers worth keeping (which SDK made the call, client correlation ids — `user-agent`, `x-request-id`, the OpenAI SDKs' `x-stainless-*` telemetry; never credentials), the request as actually sent to the provider (method, path and provider-native body — after model mapping, prompt injection, guardrail edits and provider adaptation), and the provider's response headers worth keeping (`x-request-id`/`request-id` for support-ticket correlation, `retry-after`, `x-ratelimit-*` / `ratelimit-*` state). Allowlists are config (`logging.client_header_allowlist` / `logging.upstream_header_allowlist`; entries are lowercase names, trailing `*` matches prefixes); the upstream body follows `store_bodies`, headers are metadata and stay. Cache hits record no upstream fields (nothing went on the wire). The dashboard's log detail shows all of it: client headers next to the request body, the upstream request and upstream response headers next to the response body.

**Auth & authorization rejections (always on)** — a failed auth or scoping check never reaches the request-log pipeline (there is no key to log against), so each one emits a single WARNING line on the `gateway.auth` logger instead: method, path, client address, the reason (the same message the 401/403 response body carries) and a redacted credential hint — 12 characters, the same shape as `key_prefix`, never the full key, enough to tell *which* key was tried. Scope rejections (IP allowlist, endpoint scopes) name the key. Example: `auth rejected: GET /v1/models from 127.0.0.1 — Incorrect API key provided. (credential sk-gw-6T37nE…)`. These lines and `gateway_auth_total{result="rejected"}` are the server-side traces of a rejected call; uvicorn's access log shows only the bare status code.

**Tracing (OpenTelemetry, opt-in)** — set `GATEWAY_OTEL_ENABLED=true` and every request emits one `gateway.chat` span covering the full lifecycle — for streaming requests the span stays open until the last chunk has been sent. The span carries request-id, model, provider, deployment, tokens, cost, cache-hit and outcome as attributes, and records retries, fallbacks and guardrail verdicts as events. FastAPI and httpx instrumentation give you the surrounding server/client spans, and W3C `traceparent` headers are honored on the way in and forwarded upstream, so traces join up end to end with your services and (if they support it) with the providers themselves.

```
POST /v1/chat/completions          ASGI server span (from the client's traceparent)
└─ gateway.chat                    model, key, provider, tokens, cost, status...
   ├─ gateway.guardrails.request
   ├─ POST <upstream>              one client span per attempt (retries visible)
   ├─ gateway.guardrails.response
   └─ events: gateway.retry / gateway.fallback / gateway.guardrail.blocked / gateway.guardrail.observed
```

`POST /v1/embeddings` emits the analogous `gateway.embedding` span (same attribute set, minus stream).

Point spans at a collector with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` (unset → they print to the console) and name the service with `OTEL_SERVICE_NAME` (default `llm-gateway`). Sampling & batching: `OTEL_TRACES_SAMPLER`/`OTEL_TRACES_SAMPLER_ARG` select the head-based sampler in-process; `OTEL_BSP_*` tune span batching (queue size, schedule delay, batch size, export timeout).

**Structured logs & trace correlation** — every request also emits one `chat request completed` log record with request id, trace id, model, provider, deployment, status, stream, tokens, cost and latency. Both formats print it to stdout: the default `text` mode renders a timestamped console line with the context as `key=value` pairs — `… chat request completed request_id=req_… model=glm-53 provider=openai status=success prompt_tokens=10 completion_tokens=7 latency_ms=812.4` — and `GATEWAY_LOG_FORMAT=json` renders the same record as single-line JSON, friendly to Loki/Elastic/any JSON ingester. The text branch installs its handler on the root logger only when nothing else has configured logging first (`basicConfig` semantics): plain `uvicorn gateway.main:app` gets per-request console lines out of the box, while anything that did configure it (gunicorn's logger class, an operator script) stays in control. With an OTLP log endpoint set the records are additionally exported through the OTel logs pipeline, stamped with the active span's trace context so they attach to the right trace in the backend. The DB log rows store `trace_id`/`span_id` too, so `/admin/logs?trace_id=<id>` finds the rows for any trace (and vice versa: the trace id from a log row locates the trace in Jaeger).

**Metrics bridge (optional)** — Prometheus scraping stays primary; when an OTLP metrics endpoint is configured, the same `gateway.*` series are additionally mirrored into OTLP for OTel-native backends.

### Full observability stack (one command)

A complete self-hosted stack — OTel collector with tail-based sampling, Jaeger, Loki, Prometheus with seven alert rules, and a Grafana dashboard — ships in `deploy/`:

```bash
docker compose -f deploy/docker-compose.observability.yml up --build

# gateway     http://localhost:8000    (the API itself; JSON logs, tracing on)
# Jaeger     http://localhost:16686   (traces — errors/slow kept, 10% baseline)
# Prometheus http://localhost:9090     (metrics + alerts: error rate, TTFT, retry storm, ...)
# Grafana    http://localhost:3000    (11-panel "LLM Gateway" dashboard, logs with trace links)
```

Alert rules included: high error rate, no successful requests, guardrail-block spike, p95 latency >10s, p95 TTFT >2s, retry storm (>50% of attempts retrying). See `deploy/prometheus/alerts.yml`.

## Operations

### Deploying (Docker)

The image is hardened for production: non-root user, `HEALTHCHECK` on `/readyz`, and a uvicorn graceful-shutdown window that matches the in-app drain (in-flight requests finish; background log/spend/shadow work is flushed before the process exits).

```bash
# Standalone: gateway + volumes for the SQLite DB and backups
# (optional Redis profile for multi-replica shared state: --profile redis)
docker compose -f deploy/docker-compose.yml up --build

# Or build just the image
docker build -t llm-gateway .
```

`deploy/docker-compose.yml` wires `GATEWAY_DATABASE_URL` to a volume, mounts `GATEWAY_BACKUP_DIR` on a volume, and uses `stop_grace_period: 35s` so the drain has time. Mount your own `config.yaml` over the baked-in default to change models/guardrails/routing without rebuilding.

### Liveness vs readiness

- `GET /healthz` — **liveness**: the process is up. Never checks dependencies (a wedged DB should not get the pod killed — that would also take the recovery away).
- `GET /readyz` — **readiness**: should traffic route here? 503 while starting up, while draining on shutdown, or when the DB is unreachable; 200 otherwise, with uptime and a circuit-breaker summary (open breakers are *informational* — the router routes around them; readiness reflects the gateway, not any single upstream).
- `GET /health` — pre-existing combined check (compat).

Orchestrator healthchecks (Docker `HEALTHCHECK`, Kubernetes probes, load balancers) should point at `/readyz`.

### Reloading configuration without a restart

```bash
curl -X POST http://localhost:8000/admin/reload -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
# or, from the host: kill -HUP <gateway-pid>
# or, in the admin UI: System tab → "Reload config"
```

The gateway re-reads `config.yaml` and swaps, in place: **guardrails** (including scopes and modes), **models / deployments / routing policy**, **pricing**, **model aliases / default_model**, and prompt-policy knobs. Requests in flight finish on the config they started with; new requests see the new one immediately.

Properties that hold:

- **Fail-safe** — a config that fails validation (unknown guardrail, bad YAML, missing file) is rejected with a 400 and the running configuration stays live. Nothing is half-applied: the new components are built before anything is assigned.
- **State survives** — circuit breakers keep their state across a reload (a config reload is not a health event), and per-key settings live in the DB, not the YAML.
- **Honest about limits** — provider credentials & URLs, the auth chain, cache/rate-limit/breaker backends, the semantic cache and telemetry own processes/connections and are **not** hot-swapped; if the new config changed any of them the response lists them in `restart_required` (the change takes effect after a restart).

### Backing up (and restoring)

```bash
curl -X POST http://localhost:8000/admin/backup -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
# {"name": "gateway-20261112-175559-787b38.db", "size_bytes": 126976, ...}
curl http://localhost:8000/admin/backups -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
curl -X DELETE http://localhost:8000/admin/backups/gateway-20261112-175559-787b38.db -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
```

Backups use SQLite's online `VACUUM INTO`: a consistent, compact snapshot of the **live** database with zero downtime — safe while traffic is being served and logged. Files land in `GATEWAY_BACKUP_DIR` (default `./backups`; a volume in compose). The admin UI's System tab has the same buttons. With a PostgreSQL `GATEWAY_DATABASE_URL` the endpoint refuses (400 `backup_not_supported`) — back the database up with the platform's own tooling (`pg_dump`) instead; a half-copy through a different mechanism would be a false sense of safety.

**Restore is offline by design** — replacing the database under a running engine is a footgun the API refuses to be: stop the gateway, replace the DB file (`gateway.db` or the volume's `/data/gateway.db`) with the backup, start it again. Point `GATEWAY_DATABASE_URL` at the restored file if you want it side-by-side.

### Graceful shutdown

On SIGTERM/SIGINT (compose `stop`, Kubernetes pod termination, `docker stop`): readiness flips off immediately (load balancers route elsewhere), then in-flight requests finish (uvicorn), then tracked background work — spend accounting, shadow mirrors, prompt A/B — is drained (bounded at 10s), then the log queue, OTel batches, HTTP clients and finally the engine are closed. Nothing that was acknowledged to a client is lost on the way down.

## Running tests

```bash
uv run pytest
```

The suite covers guardrails (including scoping, observe mode and per-key overrides; the wider request-phase surface — redirect with allowlist re-validation, field rewrites, modification chaining; mid-stream rewriting with boundary-spanning PII, reserve flushing, cache/log effects, source-text visibility for block checks; per-route scoping), model aliasing, per-key model routing, managed system prompts (all four inbound formats, cache identity, guardrail visibility), prompt templating (known-names-only substitution, per-key overrides, native formats), the prompt library (CRUD + activation/rollback, fail-closed refs, cache TTL, A/B mirroring with candidate-never-serves, stats/spend exclusion), log retention + PII-safe logging, budget windows, provider translations (incl. the Azure adapter), shadow deployments, the full pipeline (via a mock upstream), embeddings, images and audio (generation, edits incl. b64 input + mask + multi-image, variations, transcription/translation/TTS: multipart forwarding with unknown-field passthrough, per-image / per-minute / per-char cost metering, fallback from unsupported providers, Gemini image translation incl. edit inputs, streamed speech with post-stream accounting, Gemini TTS voice mapping + PCM/WAV, size caps, scope enforcement, bytes never logged), multimodal chat (audio input across all four inbound formats, the Anthropic refusal + routing fall-through, Gemini inlineData round trips, image-to-image chat incl. the native renderers and the streaming refusal, the TTS voice map's totality over OpenAI's voice set; audio output — OpenAI modalities/audio passthrough incl. streamed delta.audio, Gemini translation + WAV/pcm16 serving and streaming refusal, the native-Gemini audio round trip, and guardrail block/redact on transcripts in both the response and stream phases), native Anthropic/Gemini inbound formats (incl. the native embeddings endpoints: single/batch, scope mapping, quality-field and batch-mismatch 400s, Gemini southbound), the Responses API bridge and its native southbound mode (round trips, tool calls, fallback-on-modification, cache replay), agent-compat details (model retrieve, cache identity, response_format enforcement, CORS, `api-key` header), auth — the pluggable chain (JWT with HS256 and JWKS-verified RS256, custom steps), audit webhooks and token scoping — rate limits, budgets, cache (exact + semantic), fallback routing, budget alerting, the admin API + admin UI, the Redis backends (rate limiter/cache semantics and shared circuit-breaker state across two app replicas), usage export, background queued logging (drop-on-overflow, drain-on-stop) and per-key metrics (cardinality cap, shadow exclusion), plus OpenTelemetry tracing (span tree, W3C propagation, streaming span lifetime, TTFT metrics), the batch API (upload/retrieve round trip + size caps, the full lifecycle with output/error JSONL and custom_id ordering, failing lines incl. guardrail blocks and 404 error envelopes, partial waves + cancel, cursor resume, deferral on rate limits and budgets + resume after a budget raise, completion-window expiry, ownership 404s, synchronous input validation, scope enforcement, disabled config), and the operations tier (liveness/readiness semantics incl. drain + DB-down, hot-reload — guardrail/model/alias swaps, invalid-config refusal, breaker-state preservation, restart-required reporting — SQLite backups incl. snapshot validity, and the deployment artifacts' contracts).

Optional real-SDK checks (beyond the test suite, which needs no network): `uv run python scripts/sdk_compat_check.py` for the OpenAI SDK, `uv run --with anthropic python scripts/anthropic_sdk_check.py` / `uv run --with google-genai python scripts/gemini_sdk_check.py` for the native formats, and `uv run --with openai-agents python scripts/agents_sdk_check.py` for the Responses API bridge (raw OpenAI SDK + the OpenAI Agents SDK, streamed and non-streamed, incl. the tool-call roundtrip).

The suite also runs against a real PostgreSQL server: `GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/postgres uv run pytest` gives every test its own database (created and dropped around the run) — the two `VACUUM INTO` backup tests skip there, and a dedicated test pins the `pg_dump`-pointing refusal.

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.12–3.14 against SQLite (no services to boot, the suite is fully self-contained) and on Python 3.12 against a PostgreSQL 17 service container. The workflow activates as soon as the repository is pushed to GitHub.

## Design notes & limitations (MVP)

- **Storage** — SQLite via async SQLAlchemy by default (`gateway.db`, zero-config). PostgreSQL is a first-class alternative: `pip install 'llm-gateway[postgres]'` (asyncpg) and `GATEWAY_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db`. JSON columns map to JSONB there, the schema is created by the same `create_all`/auto-upgrade path, and pooled connections are pre-pinged so server-side drops recycle instead of surfacing as 500s. The full test suite runs against either backend (`GATEWAY_TEST_DATABASE_URL` opts it into a real PostgreSQL server; CI runs both). Online backups (`POST /admin/backup`) are SQLite-only — on PostgreSQL the endpoint refuses with a 400 pointing at `pg_dump`.
- **Pluggable auth chain** — `auth.chain` is an ordered list of authenticators (built-in `admin_key`, `virtual_key`, `jwt`, or `"pkg.module:ClassName"` custom steps — the same dynamic-loading story guardrails have; unknown entries fail at startup). Each step gets the raw credential exactly as extracted (Bearer / `x-api-key` / `x-goog-api-key` / `api-key` / `?key=` — one credential namespace for every inbound format) and either returns a principal, declines (`None`), or positively rejects (`AuthError` → 401). The `jwt` step proves *identity only*: its `key_claim` (default `sub`) names a virtual key whose row supplies all policy — budgets, limits, model allowlists, spend and logs accrue to that key exactly as its raw key would, and admin rights only if that key has them. Tokens verify against a shared HS256 `secret` or a `jwks_url` (cached, refreshed once on unknown `kid` for rotation; `exp` always required, `iss`/`aud` when configured, `leeway_seconds` for clock skew, algorithm allowlist — `"none"` never). A JWKS endpoint that is down fails closed with an honest 503, not a 401 blaming the client. Which step authenticated lands on every request-log row (`auth_method`) and audit record, and on `gateway_auth_total`. Requires the `auth` extra (`pip install 'llm-gateway[auth]'`).
- **Audit webhooks** — security-relevant admin actions (virtual key create/update/revoke, circuit-breaker reset) POST one JSON record each to `audit.webhook_url` (or `AUDIT_WEBHOOK_URL`): event, actor (key name + auth method — a JWT-admin is distinguishable from the admin key), details (which fields a PATCH touched, which key, which deployment) and a timestamp. Records are emitted only after the DB commit and delivered by a background task — a dead SIEM never slows or fails an admin call — best-effort with one retry, counted on `gateway_audit_events_total{event,outcome}` (`skipped` when no URL is configured, so silent auditing is visible in `/metrics`). The raw key of a newly created virtual key is never part of any record.
- **Token scoping** — a virtual key can carry `allowed_endpoints` (`chat` / `embeddings` / `images` / `audio` / `models` / `realtime` / `mcp`; `None` = all, an explicit-null PATCH clears) and `allowed_ips` (plain IPs or CIDR blocks, IPv4/IPv6; `None` = any source). `chat` deliberately covers every chat-shaped surface — OpenAI chat, the Responses bridge, and the native Anthropic/Gemini formats — since they all exercise the same machinery; `realtime` is its own scope because a WebSocket session is a different kind of commitment (long-lived, holding upstream capacity), and so is `mcp` (driving external servers, not the gateway's models). Scope names are validated at set time (a typo is a 400, not a silently-never-matching restriction); malformed IP entries restrict rather than open. Both checks run right after the auth chain succeeds (so JWT callers inherit them — the key row is the single source of policy), return 403 with `endpoint_not_allowed` / `ip_not_allowed`, and are never applied to admin contexts. They are enforced from `request.client.host`, i.e. the direct peer — put the gateway behind a trusted proxy only if that proxy is trusted to set the client address.
- **Tool-call policy** — the agent-era sibling of model allowlists: `allowed_tools` on a virtual key (and pooled on its tenant) is a list of fnmatch patterns over the tool *names* a request may declare (`None` = all tools, an explicit-null PATCH clears — exactly `allowed_models`' mechanics). One shared check runs right after model permission, and every inbound surface funnels through it because all of them — OpenAI chat, the Responses bridge, native Anthropic, native Gemini — normalize `tools` into the OpenAI shape before the pipeline. Two layers like every ceiling: the key's patterns (platform admins bypass) and the tenant's (no bypass, binds every member; a tool must pass **both**, so the tenant can only narrow). A request declaring a tool that matches neither list fails with a 400 `tool_not_allowed` naming up to five offenders — the caller sent the names, so there is nothing to leak and a debuggable error beats a mysterious one. Guardrails can modify `tools` (request-phase `modified_request_fields`); the policy is re-checked after the guardrail phase, so a guardrail can never inject a tool past the key's allowlist — the same hard boundary model redirects get. A tool whose name cannot be extracted (an exotic future tool type) fails closed as `<unnamed>`. Requests without tools pass trivially, and callers with no `allowed_tools` anywhere are byte-for-byte unchanged.
- **MCP proxy** — `POST /v1/mcp/{server}` speaks the MCP Streamable HTTP transport (2025-06-18) and transparently proxies to operator-configured servers: the same key namespace and gates as every surface, plus per-name tool policy and guardrails over the tool traffic (see the [MCP proxy](#mcp-proxy) section for the wire contract). Deliberately out of scope: **stdio** upstreams (the transport is HTTP-only — a local-process server cannot be reached from a containerized gateway anyway), the **standalone GET stream** (server→client traffic outside an open POST; GET answers 405, which the spec permits — sampling/elicitation still works *within* an exchange), and **BYOK** on MCP upstreams (there is no per-tenant notion of "your" weather server — upstream auth is the operator's `headers`/`headers_env`, and the caller's gateway key never travels upstream). Sessions are gateway-issued ids mapped to the upstream's — a client id is useless upstream, and the registry is the gateway's own state (survives hot-reloads; a removed server evicts lazily on the next request, 404, re-initialize). Policy denials are **in-band** JSON-RPC errors (HTTP 200, `-32001` tool / `-32002` guardrail) because the caller's JSON-RPC layer must see which tool and why — a transport 4xx would hide it; *gateway* failures (auth, scope, config, rate limit) keep the OpenAI envelope, the documented split. No TPM/budgets: MCP messages carry no tokens and no spend — RPM applies (the realtime precedent), token economics do not.
- **Log retention & PII-safe mode** — `logging.retention_days` prunes old request log rows in a background task (runs at startup, then every `retention_interval_seconds`; rows counted on `gateway_logs_pruned_total`); `logging.store_bodies: false` never writes request/response bodies to the DB in the first place. Neither affects the exact/semantic caches or metrics, which are in-memory (and unaffected by row deletion).
- **Background logging is opt-in** — `logging.background_write` moves the log-row INSERT (the last DB round trip a response otherwise awaits) behind a bounded queue with a background consumer. It is deliberately *not* the default, because it changes an invariant the rest of the system leans on: by default a returned response implies its log row is already queryable (the admin UI, exports and tests read `/admin/logs` right after a request). With the queue, the row lands milliseconds later; under sustained pressure a full queue drops rows (counted on `gateway_logs_dropped_total`) rather than pushing back-pressure into request latency, and shutdown drains everything pending. Turn it on when you need the hot-path headroom and can tolerate the gap.
- **Per-key metrics are opt-in and capped** — `metrics.per_key` splits the existing request/token/cost counters by virtual key, exactly paralleling the global series (same emission points, same status labels; shadow mirrors excluded like `/admin/stats`). Key names are unbounded label cardinality — a leaked-then-rotated key naming scheme would grow the series forever — so the first `per_key_max_series` distinct keys get their own label and everything after that collapses into `__overflow__` (counted on `gateway_key_label_overflow_total`). Dashboards that need exact per-key numbers over long horizons should use the request-log rows / usage export instead.
- **Rate limiting / cache / breaker backends** — in-memory by default (per-process, fine for single-worker dev); switch to Redis with `rate_limit.backend: redis` + `cache.backend: redis` + `routing.breaker.backend: redis` (and `redis.url` / `REDIS_URL`) for multi-worker/multi-replica deployments. Redis preserves the exact semantics: sliding-window RPM/TPM over sorted sets, exact-match cache with server-side TTL and an LRU size bound, and the same three-state circuit breaker with WATCH/MULTI-transaction updates (one replica tripping a deployment opens it fleet-wide; a Redis outage fails the breaker *open* — routing continues, circuits just stop protecting). With the in-memory backends, each process enforces its own window (effective limits are multiplied) and trips its own circuits.
- **Semantic caching** — opt-in (`semantic_cache.enabled`): after an exact-cache miss, the normalized request is embedded (one extra embeddings call per uncached request, routed through the normal machinery and billed to the gateway's provider key, not the caller's) and the best same-model cached response with at least `threshold` cosine similarity is served (`X-Gateway-Cache: SEMANTIC`, `gateway_cache_hits_total{result="semantic"}`). Semantics mirror the exact cache: global scope (entries shared across keys, matching only within a model), hits count toward RPM but not TPM/spend, only successful responses with usage are stored, and streams replay from the stored response. The threshold trades false-positive hits (a similar-but-not-identical answer served, cheaper) against missed hits; tune with real traffic before enabling broadly. The store is per-process memory (TTL + LRU bound) — a Redis/vector-store backend is a drop-in extension point (`embed`/`get`/`put` interface). Embedder failures degrade to a miss and never fail the request.
- **Per-key model routing** — each virtual key can carry `model_mappings` (requested name → public model) and a `default_model`, set at creation or via PATCH (explicit `null` clears them). Resolution order: the key's mapping (it wins even over real public model names — an explicit admin decision for one key is exactly what per-team steering means) → the real public name → global `model_aliases` → the key's `default_model` → the global `default_model` → pass-through. Everything downstream of resolution (allowlist, budgets, rate limits, guardrail scopes, cache identity, the echoed `model`) sees the resolved name; the log row's stored request body keeps the asked name. Like global aliases there is no admin-time target validation — a broken target surfaces as a 404 naming the target at request time. `GET /v1/models` lists a key's mapping names alongside the public models it may use (a mapping whose target the key cannot use is not exposed), and `GET /v1/models/{name}` echoes the asked name.
- **Managed system prompts are opt-in and client-wins** — `prompts.system_prompt` / per-key `system_prompt` only prepend when the request carries no system message of its own. That boundary is deliberate: the gateway manages the prompt for callers that *don't* provide one (third-party apps you can't change, internal tools with no persona support); a client that sends its own system message (`system`, `developer`, Anthropic `system`, Gemini `systemInstruction`, Responses `instructions` — all normalize to the same check) is never clobbered. Injection happens after model resolution but *before* guardrails and cache lookup on purpose: guardrails must see the prompt that actually goes out (a managed prompt that trips the injection heuristic is blocked like any client prompt would be), and the injected prompt is part of cache identity — two keys with different personas get different entries. The log row keeps the client's original body (`request_body`), as always.
- **Prompt templating only substitutes known names** — `{{variable}}` placeholders resolve against `prompts.variables` merged with the key's `prompt_variables` (key wins, the established override pattern). Unknown placeholders and literal braces pass through untouched: enabling templating can never corrupt a prompt that contains code snippets or JSON examples, because only names defined in the variables map are replaced. Resolved values are part of cache identity — the same template through two keys with different variables is two entries, not one shared answer.
- **Prompt-library references fail closed; candidates never serve** — a key's `system_prompt_ref` must name a prompt with an active version: a missing name is a request-time 400 (`prompt_not_found`, like a broken model mapping) rather than a silent fallback to some other prompt — an explicit admin decision for one key must not quietly degrade. Resolution order is ref → inline `system_prompt` → global `prompts.system_prompt`, and a client-supplied system message still wins over all three. Resolution is cached in-process (`prompts.library_cache_seconds`, default 30 s): admin-API writes invalidate immediately, and the TTL is the backstop for other replicas. The A/B **candidate** is only ever *mirrored to* — a fraction of successful requests replays against the same deployment with the candidate swapped into the system slot (own log row sharing the primary's request id, flagged `shadow` + `prompt_variant`; `gateway_prompt_mirror_*` metrics; excluded from stats and spend, exactly like shadow deployments). Promoting the candidate is activating it — and rollback is activating the old version back. The mirror skips when the client supplied its own system message (the library never served) or a guardrail rewrote the injected one (the comparison would be polluted).
- **Native Responses southbound falls back rather than dropping policy** — a deployment with `native_responses: true` forwards Responses-bridge requests to the provider's `/responses` endpoint in their original shape (fields the chat conversion drops — `store`, `truncation`, ... — survive), then translates the response back into the chat shape, so guardrails, cache, budgets and logging stay exactly on the internal spine. The original body is only forwarded while the messages are still the pristine parse: if a guardrail modified the content (PII redaction) or the prompt policy injected a system message, the adapter sends the chat-shaped conversion *carrying those modifications* instead — wire fidelity is never bought with a silently-dropped policy. Plain chat requests ignore the flag entirely, and on flag-off deployments the bridge behaves as before.
- **Rate-limit semantics** — RPM counts every accepted request (including guardrail-blocked and cache-served ones). TPM counts the reported usage of completed upstream calls: cache hits add RPM but not tokens, consistent with them having no upstream cost. 429 responses include a `Retry-After` header. Pre-flight TPM uses a `chars/4` token estimate for the request about to start.
- **Guardrails are heuristic by default** — regex/keyword based on purpose. `presidio_pii` (optional `guardrails` extra) is the production-grade PII detector; the framework is designed so you can plug real moderation APIs (Lakera, AWS Guardrails, ...) as custom `Guardrail` subclasses without touching the core. Any guardrail can be flipped to `mode: observe` to measure would-be verdicts before enforcing.
- **Stream rewriting is reserve-based, not clairvoyant** — a rewriter cannot know what the next chunk will contain, so `pii_redaction`'s `mask_in_stream` holds back the last `stream_reserve_chars` (default 100) of each delta and never emits a match that might still grow; the trade-off is that a PII span longer than the reserve (an email with a very long domain) can be split — raise the reserve for those. Latency impact: the tail of the output is delayed until it leaves the reserve zone (or the final flush before the finish chunk). `presidio_pii` does not re-analyze streamed output per chunk. Block-style `check_stream_delta` still judges the **source** text, so moderation and rewriting compose without feedback loops.
- **Presidio NLP mode is heavyweight** — the full `AnalyzerEngine` loads a ~425 MB spacy model per process (shared across guardrail instances via an engine cache). Use `use_nlp: false` for a pattern-only engine, or run the guardrail in observe mode first to check its verdict rate.
- **Embeddings** — `POST /v1/embeddings` with the same policy pipeline as chat (keys, allowlists, RPM/TPM, budgets, retries/fallbacks, breakers, cost, logs, exact-match cache). Guardrails do **not** run on embedding inputs (the phases are typed over chat messages); Gemini embeddings usage is estimated (~chars/4) because the API returns no token counts. Pre-tokenized input (arrays of token ids) is rejected with a 400.
- **Images & audio** — the modality endpoints get the same policy pipeline as embeddings (keys, allowlists, RPM/TPM, budgets, retries/fallbacks, breakers, cost, logs — no guardrails, no response cache; `X-Gateway-Cache: NONE`). Cost is unit-based: per generated/edited image, per transcribed minute, per 1M speech characters (`pricing:` keys `image_usd` / `audio_usd_per_minute` / `speech_usd_per_1m_chars`, same longest-prefix match as tokens). Provider support: OpenAI + Azure for everything (generations, edits, variations, transcription/translation, TTS); Gemini generates and **edits** images via `generateContent` translation (`n` = `n` sequential calls; `size`/`quality` and `mask` have no Gemini equivalent and are ignored) and **speaks TTS natively** (OpenAI voice names map to Gemini prebuilt voices — approximate, deterministic pairing; unknown names pass through — raw PCM output served as WAV, or raw with `response_format: pcm`; mp3 & friends are not transcoded; `speed` is ignored); Anthropic has no image/audio APIs — its deployments return a clear 501 and routing falls through. Transcription billing is duration-based and only `verbose_json` responses report a duration (plain `json` costs nothing — use `verbose_json` when billing matters). Speech streams as the provider sends it (a failure after audio started aborts the connection — HTTP cannot change status mid-body). Uploaded files are never persisted — log rows store names and sizes; speech responses are logged as a note with media type and byte count.
- **Audio & images in chat** — `input_audio` parts (OpenAI's gpt-4o-audio shape) are the gateway's internal representation and work on every inbound format: OpenAI chat passes them through to OpenAI/Azure, the Responses bridge accepts `input_audio` input parts, and native Gemini `inlineData` audio translates to them (and back). Southbound: Gemini gets `inlineData` audio; Anthropic has no audio input at all — its deployments refuse with a clear 400 (`audio_input_not_supported`) and the routing group falls through to an audio-capable one. Guardrails, managed prompts and token estimation operate on the flattened text, where audio appears as `[audio]` (so TPM pre-flight under-counts audio — real billing uses provider usage). Image-to-image chat: a Gemini image model on the chat surface (`responseModalities: TEXT+IMAGE`, set automatically for models with `image` in the name) returns generated images as `image_url` content parts in `message.content`; native Gemini responses render them back as `inlineData`, the Anthropic-native surface renders a `[generated image: …]` text note (its assistant messages cannot carry images), and streaming is refused with a clear 400 (an image has no chunk-delta representation — retry with `stream: false`). Chat request bodies are logged verbatim when `store_bodies` is on, including base64 audio/image parts (same policy as image data URLs; the multipart upload surfaces log metadata only).
- **Audio output in chat** — `modalities: ["text", "audio"]` + `audio: {voice, format}` (gpt-4o-audio shape) ask for speech; the reply carries `message.audio` (`transcript` + base64 `data`, `content` null), streamed as `delta.audio` chunks. OpenAI/Azure take both fields verbatim and pass responses through; Gemini translates to `responseModalities: [TEXT, AUDIO]` + `speechConfig` (voice via the TTS voice map — all 11 OpenAI voices, every Gemini prebuilt by name) and serves the reply's PCM as WAV (`format: wav`, the default) or raw (`pcm16`); mp3 & friends are not transcoded. Native Gemini inbound accepts the same fields through `generationConfig` (and renders `message.audio` back as text + `inlineData`, RIFF-sniffed). Anthropic has no audio output — its deployments refuse with a clear 400 (`audio_output_not_supported`) and the routing group falls through; the Anthropic-*native* surface renders an audio reply's transcript as its text block. Audio-output chat does not stream on Gemini (raw PCM has no chunk-delta representation without re-framing WAV headers mid-stream — retry with `stream: false`). Guardrails are not bypassed by speech: response checks see the transcript (`assistant_text`), streamed `delta.audio.transcript` deltas feed the stream-phase checks, and the redactors redact transcript text (the audio bytes themselves are never rewritten — re-synthesis is out of scope). The Responses bridge has no audio-output request field; an audio reply reaching it renders the transcript as `output_text`. Audio responses participate in the exact-response cache like any other body.
- **Native Gemini embeddings** — `POST /v1[/beta]/models/{model}:embedContent` and `:batchEmbedContents` parse to the internal embeddings request and run the same pipeline (allowlists, rate limits, budgets, caching, retries/failover, logs), rendering replies as `{embedding: {values}}` / `{embeddings: [{values}]}`. The paths carry the `embeddings` token scope. `taskType` and `title` are refused with 400s (they change embedding quality — fail loudly, not silently wrong), as are batch sub-requests with mixed `model`s or `outputDimensionality` (the gateway routes the whole batch to one deployment); `outputDimensionality` maps to `dimensions`, and sub-request `model` fields (native names) are ignored — the URL model routes.
- **Batch API** — `/v1/files` + `/v1/batches` execute bulk chat workloads in the background (see the [Batch API](#batch-api) section for the flow). Deliberate scope limits: only `POST /v1/chat/completions` lines (no embeddings/images batches — `url` is validated), only the `24h` completion window, and `stream` in line bodies is stripped (batches never stream — a batch has no client connection to stream to). Execution is sequential *waves* (`wave_size` per `poll_interval_seconds`) through the full interactive pipeline — no parallel fan-out; a batch is a trickle, not a burst. Deferral semantics: rate limits and budget ceilings stop a wave and reschedule the batch (`retry_not_before`) — lines never fail for those, and the window bounds deferrals into `expired`; budgets are evaluated per wave against a fresh key snapshot (the same auth-time-consistency trade interactive requests make — spend from earlier lines of the same wave is visible from the next wave on). Files have no deletion endpoint and no expiry sweep — delete old input/output files with SQL against the `files` table if storage matters (batches reference their input file by id; deleting it just fails the batch at its next wave). The worker is per-process (multi-replica deployments would each run waves — safe: the cursor advance is guarded — but for paced batches point one replica at the workload or raise `poll_interval_seconds`). No FK constraints between batches and files/results: ownership is enforced in the API layer (404s), keeping the schema portable and the auto-upgrade path simple.
- **Spend accounting** for streaming/non-streaming requests happens after the response is sent; budget checks use the spend known at request time.
- **Budget windows** — `daily_budget_usd` / `monthly_budget_usd` on a virtual key cap spend per **UTC calendar** window (day rolls at 00:00 UTC, month on the 1st), composing independently with the total `max_budget_usd`. Each window's spend and bucket id live on the key row; when the bucket has rolled, the stored spend counts as 0 and the next spend write resets it in the same atomic UPDATE — concurrent requests can never straddle a boundary. Exceeding any cap is the same `429 insufficient_quota` (message names the period). Cache hits add nothing to any window. Budget alerts remain total-budget only (no windowed webhooks).
- **Projected budget enforcement (tier 50)** — the gate leg the hard caps lack: a key at $8 of a $10 monthly budget on day 2, burning at a month-end pace of $120, sails under the hard cap until actual spend crosses $10 — then 429s with the money already gone. With `budget_enforcement.enabled: true`, a key or tenant whose month-end **run-rate projection** exceeds its `monthly_budget_usd` is denied earlier, with the same `429 insufficient_quota` and a message that says it is the projection, with the numbers:

  ```yaml
  budget_enforcement:
    enabled: true
    min_elapsed_days: 2.0   # the honesty gate: hours of evidence never deny
  ```

  The rules, in order: the hard caps are the floor and fire first (a projection denial only ever happens with spend still under budget — the two messages can never lie about each other); nothing spent projects zero and passes; a stale bucket is last month's spend and counts as 0 (the stance composes with the rollover); `min_elapsed_days` (default 2.0, the forecast family's threshold from tiers 47/48) keeps a day-one straight line from denying; and the denial **self-heals** — spend over elapsed days is a ratio, so a caller that genuinely stops spending watches its projection decay toward the actual as the month elapses and traffic resumes with no operator action, while the hard cap stays the wall it always was. Key and tenant legs share one rulebook (one helper, `_check_projected_budget`); the projection reads the auth-time spend snapshot — the same counter lens the hard cap reads, never the billing lens — so the two legs can never disagree about what "spent" means, and the check costs no extra query on the hot path. It applies on every surface the budget gate already runs (chat incl. native formats, realtime per create, embeddings, images, audio); off by default — today's behavior is exactly preserved until you opt in.
- **Shadow deployments** — a deployment with `shadow: true` never serves clients (the router excludes it; a model whose deployments are all shadow answers 503). After a *successful* primary response, `shadow_fraction` (default 1.0, clamped) of requests is mirrored to it in the background: always non-streaming `complete()` (even for streamed requests, for comparable latency/status/content), never affecting the client. Mirrors skip guardrails, rate limits, budgets and the caches; results land in `gateway_shadow_requests_total` / `gateway_shadow_latency_seconds`, on the deployment's circuit breaker, and in request-log rows flagged `shadow` (own cost and bodies, sharing the primary's request id) which are excluded from `/admin/stats` and never add to key spend. Mirroring is chat-only (embeddings are not mirrored). Since tier 35 every mirror is also *scored* against the primary's raw provider text (see [Shadow diffing](#shadow-diffing)): the row carries `shadow_verdict` + `shadow_similarity`, and `/admin/shadow-diff` aggregates the pairs into per-deployment migration evidence. **Canary** rollouts use `weight` instead: real traffic, shifted gradually within a same-priority tier. When passive mirroring isn't enough — not enough traffic, or the candidate isn't wired as a shadow — [candidate trials](#candidate-trials) replay a captured window against any deployment deliberately.
- **Budget alerts** — when a key's spend crosses a threshold of its `max_budget_usd` (default 50/80/90/100%), a JSON webhook (`type: budget_alert`, with key, spend, budget, percent and a human message) is POSTed once per level per key. Fired levels persist on the key row, so alerts survive restarts and stay correct across workers; raising a key's budget re-arms the thresholds below the new spend. Evaluation and delivery run in the background spend task — they never block or fail requests — and delivery is best-effort: one retry with backoff, failures logged and counted on `gateway_alerts_total{outcome="failed"}`. With no `webhook_url`/`ALERT_WEBHOOK_URL` configured, crossings are only counted as `outcome="skipped"`.
- **Key-expiry warnings** — the same webhook (`type: expiry_alert`), deadline edition: a background sweep (hourly, re-reading `alerts:` each pass) walks active keys with an `expires_at` and fires once per day-level (`alerts.expiry_days`, default 14/7/1/0; 0 = the expired page) as the deadline approaches, highest first. State persists on the key row beside the budget levels, before delivery (duplicate silence over duplicate noise). Setting or moving a deadline arms silently — every level already crossed is marked fired, the budget re-arm rule for deadlines — so a key born 6 days from its deadline never pages "expires in 14 days" for a choice the operator just made; a key the sweep *discovers* near its deadline (fired state NULL — it predates the feature) fires honestly. Revoked keys and deadline-less keys never warn; rotation never touches the state — a deadline is a deadline whichever credential presents it. With no webhook URL, crossings count as `kind="expiry", outcome="skipped"` while the `expiry.threshold` observer events still fire.
- **Multi-tenancy** — tenants are organizations of virtual keys carrying their own policy ceiling (`allowed_models`, `allowed_tools`, RPM/TPM, total/daily/monthly budgets — the same budget machinery as keys, pooled across every member). A member key must pass **both** its own checks and the tenant's; the tenant can only narrow, and there is deliberately **no admin bypass** at the tenant layer — a tenant admin (`is_admin` key with a `tenant_id`) manages that tenant's keys and reads its logs/stats/usage, but is still a member of it. Tenant scoping is 404-based for cross-tenant ids (confirming another tenant's key exists would leak it) and 403-based for platform endpoints (tenants CRUD, prompts, routing, guardrail inspection, reload, backups — `require_platform_admin`); keys are re-homed only by platform admins (tenant admins' creates are forced into their own tenant, a mismatching explicit `tenant_id` is a 400). Auth fails closed: a dangling `tenant_id` (corrupt state) or `is_active: false` (the platform admin's kill switch) rejects every member key with 401 — a disabled tenant's keys are never served as unrestricted platform keys. Spend accounting writes both rows (key and tenant) in separate transactions; rate limits are two windows (the key's own and the shared `tenant:{id}` one, TPM included); budget alerts fire per level on either, with `kind: "key" | "tenant"` in the payload and fired levels persisted on the matching row. Data-plane responses carry `X-Gateway-Tenant` (the slug); log rows and the usage export carry `tenant_id` (NULL = platform key). Cache identity is tenant-salted: the exact/semantic chat caches fold in a **response-policy token** (the tenant plus every response-capable guardrail active for the caller, so a key's `disabled_guardrails` or a scoped guardrail splits entries) — a hit skips response guardrails, so callers share entries only when the policy that shaped them matches; the embeddings cache is salted by tenant alone (no guardrails run there).
- **BYOK vault** — tenants can store their *own* provider credentials (encrypted at rest under `GATEWAY_VAULT_MASTER_KEY` — env only, never config.yaml, since it encrypts secrets) and their requests then ride their account instead of the platform's configured key: one credential per (tenant, provider), resolved per candidate inside the retry/fallback loop (so the tenant's key never leaks onto a fallback provider they didn't key — and with `vault.strict: true` it *cannot*: unkeyed deployments are removed from the tenant's ladder entirely, retries and fallbacks stay inside the keyed providers, and a model with no keyed deployment refuses 403 `byok_key_required` naming exactly what to key; platform callers and the platform's own machinery are untouched). The raw key exists exactly twice — the `PUT` body and the upstream auth header; every management surface speaks a hint (`…1234`), audit records carry the hint, log rows only the provider name (`byok_provider`, `X-Gateway-Byok-Provider`, `gateway_byok_requests_total`). Fail closed on undecryptable entries (`byok_key_unreadable` — a rotated-away master key is a 500, never a silent platform-key fallback); fail honest when disabled (no master key → `PUT` 400s `vault_unavailable`, reads stay empty, callers unchanged). Decrypted entries are cached in process memory for `vault.ttl_seconds` with invalidate-on-write; `vault.backend: redis` publishes each invalidation so every replica (and every replica's *negative* "no key stored" cache twin) converges immediately instead of within the TTL — best-effort both directions, the TTL the backstop. Managed end to end from the admin UI's Vault tab (`GET /admin/vault` status + the credential manager, pinned to the tenant for tenant admins); the strict stance hot-reloads live while the backend/TTL report `restart_required`. Known sharp edge, documented: strict with no master key means tenant traffic cannot run at all — the vault cannot decrypt what no key encrypted.
- **Usage export** — `GET /admin/usage/export` is a billing/chargeback extract over the request log: rows in `[since, until]` (ISO-8601, naive timestamps read as UTC, default the last 30 days), filterable by `key_name`/`model`/`status`. CSV (stable column order, attachment download) or JSON (rows + a `summary` with totals and by-status/by-key/by-model aggregates, most-expensive-first). Mirrored shadow calls are excluded by default — they never cost the client anything — and included flagged with `include_shadow=true`; cache-hit rows appear with their cached token counts and 0 cost. The row list is capped by `limit` (default 100k, max 250k; the `truncated` flag says so) while aggregates always cover the whole window. The admin UI's "Export usage (CSV)" button downloads the selected window.
- **Circuit breakers** are per-process by default (each worker/replica trips and recovers on its own evidence); `routing.breaker.backend: redis` shares the state so one replica's trip skips the deployment fleet-wide, with `/metrics` scrapes and admin views re-syncing from shared state. If Redis is down, the breaker fails *open* (routes anyway) — protection, not correctness. For streams, only failures *before the first chunk* trip the breaker: once a stream is committed, later upstream errors surface to the client per the failover contract.
- **Admin UI** — a read-mostly operations tool, not an audit console: it edits keys, resets breakers and expands log rows (including stored request/response bodies) on top of the JSON admin API. The shell itself needs no auth because it contains no data; the admin key is pasted into a login overlay, kept in the browser tab's `sessionStorage` and sent as a bearer token. The page is fully self-contained (no CDN, fonts or scripts fetched — enforced by a test) so it works in air-gapped deployments, at the cost of hand-rolled vanilla JS in one file.
- **Observability** — Prometheus metrics at `/metrics` (scraping is the primary path; an optional bridge mirrors the series into OTLP), OpenTelemetry tracing with head-based sampling in-process and tail-based sampling at the collector (`deploy/`), JSON stdout logs + an OTel logs pipeline with trace-context stamping, and DB log rows carrying `trace_id`/`span_id` for trace ↔ log correlation. A full self-hosted stack (collector, Jaeger, Loki, Prometheus, Grafana dashboard + alert rules) ships in `deploy/`. Remaining gaps: no Alertmanager wiring for the Prometheus rules (budget alerts go via webhook instead), and per-key metrics exist only as DB aggregates, not Prometheus series.
- **Native inbound formats are translated, not passed through** — the gateway normalizes Anthropic/Gemini requests to its internal OpenAI shape and back. Fidelity notes: Anthropic `top_k` is dropped (no OpenAI equivalent); Gemini `candidateCount > 1` is rejected (one choice served); Gemini function calls carry no ids, so tool-call ids are synthesized from the function name (`call_<name>`); Gemini's JSON-array streaming variant is unsupported (`:streamGenerateContent` always answers SSE, which is what the SDKs request); on Anthropic streams `message_start` reports `input_tokens: 0` (real token counts arrive with the terminal `message_delta`, since the gateway only learns usage from the final upstream chunk); request logs store the normalized internal request, not the client's original bytes. Native endpoints can still hit the same cache entries as OpenAI clients — the translation is deterministic and the cache key covers only the normalized semantic fields.
- **The Responses bridge is stateless** — `POST /v1/responses` translates each call to a chat completion: `previous_response_id` (conversation state), `background` mode, server-side tools (`web_search`, `file_search`, `code_interpreter`, `computer_use`, `mcp`) and reasoning-model outputs are out of scope and rejected with clear 400s; `store`/`truncation`/`include` are accepted no-ops. Agents must send the full conversation each turn (the Agents SDK does). Reasoning items in `input` are dropped; framework-visible behavior otherwise matches the Responses API.
- **Realtime sessions are connect-time-routed and text-guarded** — the stateful surface (`WS /v1/realtime`) makes two deliberate trade-offs. Routing runs once, at the upstream handshake: the residency- and breaker-filtered ladder is walked between handshake attempts, then the session stays on its chosen leg — there is no retry protocol for a WebSocket that is 40 minutes into a session — so a dead leg can move a *new* session to a fallback, but a leg that dies mid-session ends the session (the client reconnects, which re-runs the ladder). Guardrails apply to the **text** surfaces of the event protocol — a create's input/instructions (or the shadowed conversation when it carries none) and `output_text.delta` — while audio deltas and non-text event fields pass through unexamined (transcription-based audio inspection would be a follow-up). The request caches, idempotency and runaway detection are HTTP-request machinery and do not apply; the conversation shadow the relay keeps is in-memory and per-session. One session is one request-log row with usage summed across its responses, which also means budget re-checks (per create) see the session's running spend rather than each response in isolation — the same snapshot semantics as HTTP, over a longer-lived unit.

## Endpoint compatibility notes

- Errors use the OpenAI envelope by default: `{"error": {"message", "type", "param", "code"}}` — the native Anthropic/Gemini endpoints answer with their own envelopes instead (`{"type": "error", "error": {...}}` / `{"error": {"code", "message", "status"}}`)
- `response_format` is honored where the routed provider can honor it: OpenAI passthrough takes any shape, Gemini maps `json_object` (`responseMimeType`) and `json_schema` (`responseSchema`), and Anthropic deployments **reject** it with `400 response_format_not_supported` (no JSON mode exists there) — the router first falls through to later deployments that can honor it, so a model with an OpenAI/Gemini fallback serves the request from there
- Guardrail-blocked requests → `400` with `code: "content_policy_violation"`
- Guardrail-blocked responses → `200` with `finish_reason: "content_filter"` (OpenAI semantics)
- Rate limit → `429` `rate_limit_exceeded`; budget → `429` `insufficient_quota`; unknown/unauthorized model → `404` `model_not_found`; endpoint-scope / IP violation → `403` `endpoint_not_allowed` / `ip_not_allowed`
- Realtime sessions report the same failures in-band: every failure to open (auth, scope, model permission, rate limit, budget, no realtime-capable deployment, all handshakes refused) is an OpenAI-shaped `{"type": "error", "error": {...}}` event followed by close 1008; mid-session the same shape rides the event stream (e.g. a budget exhausted across responses, a guardrail-blocked create — which kills the response, not the session)
- MCP answers **protocol-level** failures in the JSON-RPC envelope (`{"jsonrpc": "2.0", "id": …, "error": {code, message}}`): parse/batch → transport `400` `-32700`/`-32600`, missing/unknown session → `400`/`404` `-32000`/`-32003`, tool denials and guardrail blocks → in-band at **HTTP 200** (`-32001`/`-32002`); gateway failures (auth, scope, disabled/unknown/not-allowed server, rate limit) keep the OpenAI envelope like every other surface
