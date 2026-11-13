# Changelog

All notable changes to the LLM Gateway are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

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
