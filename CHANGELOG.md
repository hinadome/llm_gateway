# Changelog

All notable changes to the LLM Gateway are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

Image generation and audio — the non-chat modalities, on the same policy
spine (keys, routing, retries, failover, breakers, budgets, rate limits,
logging).

### Added

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

### Changed

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

[0.1.0]: https://example.com/llm-gateway/releases/tag/v0.1.0
