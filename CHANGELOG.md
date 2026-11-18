# Changelog

All notable changes to the LLM Gateway are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

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
