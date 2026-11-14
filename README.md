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
| **Guardrails (in/out)** | Pluggable request/response/stream-phase checks: PII redaction (regex or Microsoft Presidio), prompt-injection heuristics, keyword blocklists, output moderation, response-schema validation, request limits. Request-phase guardrails can **modify** more than messages — rewrite `model` (reject-and-redirect: "too expensive for the flagship model, serve it on the small one"), sampling params and tools, with modifications **chaining** across guardrails. Stream-phase guardrails can **rewrite output as it streams** (PII masked mid-stream, never reaching the client, no matter how the provider chunked it). Any guardrail can run in **observe mode** (log & count would-be verdicts without enforcing) and can be **scoped** to certain keys/models/routes (`scope` with fnmatch globs; per-route policy like "strict checks on `/v1/chat/completions`, looser on the native formats"; per-key runtime overrides via `disabled_guardrails`) |
| **Model aliasing** | `model_aliases` maps client-requested names to your public model names — apps with hardcoded provider model names (`gpt-4o`, `claude-3-5-sonnet-...`) work unchanged; opt-in `default_model` catches any unknown name; real model names always win over aliases |
| **Per-key model routing** | Each virtual key can carry its own `model_mappings` (requested name → public model) and `default_model` — per-team steering without code changes. Mappings apply before global aliases and win even over real public names; everything downstream (permissions, budgets, caches, logs) sees the resolved name |
| **Managed system prompts** | A global `prompts.system_prompt` or a per-key `system_prompt` (admin API/UI, no restart) is prepended to requests that carry **no system message of their own** — the gateway manages the prompt for callers that don't provide one; a client-supplied system message (OpenAI `system`/`developer`, Anthropic `system`, Gemini `systemInstruction`, Responses `instructions`) always wins. Injection runs before guardrails and cache lookup, so the prompt that goes out is the prompt that gets checked and cached |
| **Prompt templating** | `{{variable}}` placeholders in message text (and in the managed system prompt) resolve against `prompts.variables` merged with the key's `prompt_variables` (key wins) — one persona template, different values per team. Only *known* names are replaced: unknown placeholders and literal braces (code snippets, JSON examples) pass through untouched |
| **Prompt library (prompt ops)** | Versioned named prompts in the DB (`POST /admin/prompts` + the admin UI's Prompts tab): a key can reference one by name (`system_prompt_ref`) instead of inline text — its **active** version serves, so editing a prompt is a version bump + activate (no key change, no restart, instant rollback). A second **candidate** version A/B-mirrors in the background (a fraction of successful traffic, own log rows flagged with the variant, `gateway_prompt_mirror_*` metrics — never client-facing, never in stats or spend) |
| **Inspection** | Every request/response stored with guardrail decisions, tokens, cost, latency; queryable via `/admin/logs` (filterable by trace id); Prometheus metrics at `/metrics`; OpenTelemetry tracing + logs with trace correlation (opt-in); full Grafana/collector stack in `deploy/`; PII-safe mode stores no bodies (`store_bodies: false`) and old rows are pruned automatically (`retention_days`) |
| **Admin UI** | Built-in dashboard at `/admin/ui`: key management (create/edit/revoke, budget bars, alert badges, tenant placement, admin-key grants), tenant management (create/edit/delete with pooled budget bars and the kill switch; platform admins only — tenant admins land on a scoped note), request-log browsing with full request/response inspection, routing table with live circuit-breaker state, guardrail inventory, and a System tab (health, config reload, backups) — one self-contained HTML file, no build step, no external resources (works offline) |
| **Virtual keys** | Per-key model allowlists (fnmatch patterns), RPM/TPM rate limits, USD budgets with spend tracking — total plus **daily / monthly windows** (UTC calendar caps, e.g. "no more than $5/day") |
| **Multi-tenancy** | Tenants (organizations) group virtual keys under a shared policy ceiling — model allowlist, RPM/TPM, and total/daily/monthly budgets that apply to *every* member key, tenant admins included (they manage their own tenant's keys, not its policy; a member must pass both its own checks and the tenant's — the tenant can only narrow). **Tenant admins** (`is_admin` key inside a tenant) see only their tenant's keys/logs/stats/usage (a cross-tenant id is a 404, not a 403 — no existence leaks) and are 403 on platform endpoints (tenants, prompts, routing, guardrails, reload, backups). `is_active: false` is a kill switch for every member key (re-enable restores them); pooled spend and budget alerts fire per tenant too (`kind: "tenant"` in the payload); responses carry `X-Gateway-Tenant`; caches are isolated per tenant |
| **Pluggable auth chain** | `auth.chain` lists the strategies that may authenticate a caller, in order: built-in `admin_key` / `virtual_key` / `jwt`, or your own `"pkg.module:ClassName"` step. A **JWT/OAuth bearer token** authenticates the virtual key named by its `sub` claim (HS256 secret or a JWKS with rotation) — budgets, limits and spend accrue to that key exactly as its raw key would |
| **Audit log** | Security-relevant admin actions (key create/update/revoke, breaker reset) POSTed as JSON webhooks to your SIEM/audit trail — who (key + auth method), what, when; best-effort background delivery, counted on `gateway_audit_events_total` |
| **Token scoping** | Per-key `allowed_endpoints` (`chat` / `embeddings` / `images` / `audio` / `models`; `chat` covers the native formats and the Responses bridge) and `allowed_ips` (plain IPs or CIDR blocks) — defense in depth beyond model allowlists; admins are never scoped |
| **Budget alerts** | Webhook fired when a key's spend crosses a threshold of its budget (default 50/80/90/100%, configurable); once per level per key, survives restarts, raising the budget re-arms; best-effort delivery with retry, counted on `gateway_alerts_total` |
| **Routing** | Weighted load balancing, retries with exponential backoff, cross-provider fallbacks, per-deployment circuit breakers (failing deployments are skipped, probed after a cooldown; state visible in `/admin/routing` and on `gateway_circuit_state`; per-process by default, or **shared across replicas via Redis** with `routing.breaker.backend: redis`). **Canary** = a deployment's `weight` within a tier (raise it gradually to shift traffic); **shadow** = mirror a fraction of successful requests to a non-serving deployment for comparison |
| **Usage export** | `GET /admin/usage/export` (CSV or JSON): billing / chargeback extract of request rows with cost/tokens/latency, time-window + key/model/status filters, and per-key / per-model / per-status aggregates; mirrored shadow calls excluded by default |
| **Batch API** | OpenAI-style `/v1/files` + `/v1/batches`: upload a JSONL of chat requests, execute them in the background — each line through the *full* pipeline (guardrails, cache, routing, spend, request logs) — then fetch per-line results as output/error JSONL. Rate limits and budgets defer a batch instead of failing lines; the 24h completion window bounds retries (see [Batch API](#batch-api)) |
| **Caching** | Exact-match response cache with TTL; cache hits replay for streaming requests too; optional **semantic cache** serves near-duplicate prompts by embedding similarity (configurable model + threshold) |
| **Provider translation** | OpenAI passthrough (any OpenAI-compatible base: vLLM, Ollama, Groq, ...), Azure OpenAI (deployment-addressed, `api-key` header, `api-version`), Anthropic & Gemini full request/response/stream translation incl. tools |
| **Cost tracking** | Per-1M-token pricing table (configurable), cost per request/key/model |
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

Diagnostic response headers on every data-plane response (completions, embeddings, images, audio): `X-Request-Id`, `X-Gateway-Model`, `X-Gateway-Provider`, `X-Gateway-Deployment`, `X-Gateway-Cache` (HIT/SEMANTIC/MISS; NONE on the non-cacheable surfaces), `X-Gateway-Cost-Usd`, `X-Gateway-Latency-Ms`.

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
| `PATCH /admin/keys/{id}` | Update limits, allowlist, active flag, budgets (total / daily / monthly), `disabled_guardrails`, `model_mappings` / `default_model`, prompt policy (`system_prompt` / `system_prompt_ref` / `prompt_variables`), scoping (`allowed_endpoints` / `allowed_ips`; explicit `null` clears — changing `max_budget_usd` re-arms budget alerts); `tenant_id` re-homes the key (**platform admin only**, target validated — tenant admins get 403) |
| `DELETE /admin/keys/{id}` | Revoke a key |
| `POST /admin/tenants` | Create a tenant (platform admin): slug `name` (header-safe pattern — it surfaces in `X-Gateway-Tenant` and log filters), `display_name`, model allowlist, RPM/TPM, budgets |
| `GET /admin/tenants` / `GET /admin/tenants/{id}` | List/inspect tenants, with pooled spend and fired alert levels (platform admin) |
| `PATCH /admin/tenants/{id}` | Update a tenant's policy ceiling (slug immutable) — `is_active: false` disables every member key at once; budget changes re-arm tenant alerts |
| `DELETE /admin/tenants/{id}` | Delete a tenant — refused with 409 while any key still references it (a dangling `tenant_id` fails closed at auth time, so re-home or revoke first) |
| `POST /admin/prompts` | Append a version to a named prompt (first version of a name auto-activates) |
| `GET /admin/prompts` | One summary per prompt name: active version, A/B candidate, version count |
| `GET /admin/prompts/{name}` | All versions of one name, newest first |
| `PATCH /admin/prompts/{id}` | Activate a version (promote or rollback — switches serving, no restart), mark it as the A/B **candidate** (unique per name; `ab_fraction` clamped to 0–1), or edit its notes |
| `DELETE /admin/prompts/{id}` | Delete one version (the active version is refused with 409 — activate another first) |
| `GET /admin/logs` | Paginated request logs with filters (bodies omitted for size), rows carry `tenant_id` (`?tenant_id=` filter; forced to the admin's own tenant for tenant admins); shadow-mirror rows are included and flagged `shadow`, prompt A/B mirrors also carry `prompt_variant` |
| `GET /admin/logs/{id}` | One log row in full, including the stored request/response bodies (cross-tenant ids 404 for tenant admins) |
| `GET /admin/stats` | Requests, error rate, tokens, cost, latency + TTFT percentiles (client traffic only — shadow mirrors are excluded); tenant admins see their own tenant's traffic, platform admins additionally get a `by_tenant` breakdown |
| `GET /admin/usage/export` | Billing / chargeback export: `format=csv\|json`, `since`/`until` (ISO-8601, default last 30 days), `key_name`/`model`/`status`/`tenant_id` filters, `include_shadow`; JSON adds totals + per-key/model/status aggregates (+ per-tenant for platform admins); tenant admins export their own tenant only |
| `GET /admin/routing` | The routing table (public models → deployments, with each deployment's circuit-breaker state; shadow deployments flagged with their mirror fraction) |
| `POST /admin/routing/breakers/{deployment}/reset` | Force-close a deployment's circuit (next request probes it again) |
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
        "allowed_ips": ["10.0.0.0/8", "192.168.1.15"]
      }'
# → { "key": "sk-gw-...", ... }   (shown only once)
```

`system_prompt_ref` names a prompt in the library (see the Prompts tab / `POST /admin/prompts`); its active version serves as the key's system prompt — `{{company}}` inside that prompt body resolves with the key's `prompt_variables`. Editing the prompt later is a new version + activate: no key change, no restart. An inline `system_prompt` on the same key is the fallback the ref overrides.

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
│                        # output_moderation, response_schema, request_limits
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

The pipeline re-checks the key's model allowlist after the guardrail phase — a redirect can never bypass permissions (fail closed: the request is refused, not silently rerouted). Everything downstream (routing, logs, metrics, cache identity) sees the final model, while `request_body` in the log keeps what the client originally asked for. The built-in `request_limits` guardrail exposes the same pattern from config: `on_exceed: redirect` + `redirect_model` sends oversized requests to a cheaper model instead of blocking them.

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
  breaker: {enabled: true, failure_threshold: 3, cooldown_seconds: 30}
alerts:                # webhook when a key's spend crosses a % of its max_budget_usd
  budget_thresholds: [50, 80, 90, 100]        # each level fires once per key
  webhook_url: https://hooks.slack.com/services/T000/B000/XXXX   # or url_env: ALERT_WEBHOOK_URL
  timeout_seconds: 5                          # per POST attempt
  max_attempts: 2                             # 1 retry (0.5s backoff)
audit:                 # webhook for security-relevant admin actions (key CRUD, breaker reset)
  webhook_url: https://siem.example.com/collect   # or url_env: AUDIT_WEBHOOK_URL
  timeout_seconds: 5
  max_attempts: 2
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
pricing:
  gpt-4o: {input: 2.5, output: 10.0}      # USD per 1M tokens
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
| `gateway_circuit_state` | gauge | deployment, provider — circuit breaker state (0=closed, 1=half_open, 2=open) |
| `gateway_circuit_transitions_total` | counter | deployment, provider, state — breaker trips / recoveries |
| `gateway_tokens_total` | counter | type (prompt/completion) |
| `gateway_cost_usd_total` | counter | model |
| `gateway_guardrail_blocks_total` | counter | guardrail, phase — enforce mode only |
| `gateway_guardrail_observed_total` | counter | guardrail, phase, action — would-be verdicts in observe mode |
| `gateway_cache_hits_total` | counter | result (hit / semantic / miss) |
| `gateway_alerts_total` | counter | kind, outcome — budget-threshold alerts (sent / failed / skipped-when-no-URL) |
| `gateway_auth_total` | counter | authenticator, result (authenticated / rejected) — per auth-chain step |
| `gateway_audit_events_total` | counter | event, outcome — admin-action audit records (sent / failed / skipped-when-no-URL) |
| `gateway_logs_pruned_total` | counter | request log rows deleted by the retention task (`logging.retention_days`) |
| `gateway_logs_dropped_total` | counter | request log rows dropped because the background write queue (`logging.background_write`) was full |
| `gateway_key_requests_total` | counter | key, status — requests per virtual key (`metrics.per_key`; shadow mirrors excluded) |
| `gateway_key_tokens_total` | counter | key, type — prompt/completion tokens per virtual key (`metrics.per_key`) |
| `gateway_key_cost_usd_total` | counter | key — upstream cost per virtual key (`metrics.per_key`) |
| `gateway_key_label_overflow_total` | counter | per-key labels collapsed into `__overflow__` past `metrics.per_key_max_series` distinct keys |
| `gateway_shadow_requests_total` | counter | deployment, outcome (success / error) — requests mirrored to shadow deployments (never affect the client) |
| `gateway_shadow_latency_seconds` | histogram | deployment — latency of mirrored (shadow) upstream calls |

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
- **Token scoping** — a virtual key can carry `allowed_endpoints` (`chat` / `embeddings` / `images` / `audio` / `models`; `None` = all, an explicit-null PATCH clears) and `allowed_ips` (plain IPs or CIDR blocks, IPv4/IPv6; `None` = any source). `chat` deliberately covers every chat-shaped surface — OpenAI chat, the Responses bridge, and the native Anthropic/Gemini formats — since they all exercise the same machinery. Scope names are validated at set time (a typo is a 400, not a silently-never-matching restriction); malformed IP entries restrict rather than open. Both checks run right after the auth chain succeeds (so JWT callers inherit them — the key row is the single source of policy), return 403 with `endpoint_not_allowed` / `ip_not_allowed`, and are never applied to admin contexts. They are enforced from `request.client.host`, i.e. the direct peer — put the gateway behind a trusted proxy only if that proxy is trusted to set the client address.
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
- **Shadow deployments** — a deployment with `shadow: true` never serves clients (the router excludes it; a model whose deployments are all shadow answers 503). After a *successful* primary response, `shadow_fraction` (default 1.0, clamped) of requests is mirrored to it in the background: always non-streaming `complete()` (even for streamed requests, for comparable latency/status/content), never affecting the client. Mirrors skip guardrails, rate limits, budgets and the caches; results land in `gateway_shadow_requests_total` / `gateway_shadow_latency_seconds`, on the deployment's circuit breaker, and in request-log rows flagged `shadow` (own cost and bodies, sharing the primary's request id) which are excluded from `/admin/stats` and never add to key spend. Mirroring is chat-only (embeddings are not mirrored). **Canary** rollouts use `weight` instead: real traffic, shifted gradually within a same-priority tier.
- **Budget alerts** — when a key's spend crosses a threshold of its `max_budget_usd` (default 50/80/90/100%), a JSON webhook (`type: budget_alert`, with key, spend, budget, percent and a human message) is POSTed once per level per key. Fired levels persist on the key row, so alerts survive restarts and stay correct across workers; raising a key's budget re-arms the thresholds below the new spend. Evaluation and delivery run in the background spend task — they never block or fail requests — and delivery is best-effort: one retry with backoff, failures logged and counted on `gateway_alerts_total{outcome="failed"}`. With no `webhook_url`/`ALERT_WEBHOOK_URL` configured, crossings are only counted as `outcome="skipped"`.
- **Multi-tenancy** — tenants are organizations of virtual keys carrying their own policy ceiling (`allowed_models`, RPM/TPM, total/daily/monthly budgets — the same budget machinery as keys, pooled across every member). A member key must pass **both** its own checks and the tenant's; the tenant can only narrow, and there is deliberately **no admin bypass** at the tenant layer — a tenant admin (`is_admin` key with a `tenant_id`) manages that tenant's keys and reads its logs/stats/usage, but is still a member of it. Tenant scoping is 404-based for cross-tenant ids (confirming another tenant's key exists would leak it) and 403-based for platform endpoints (tenants CRUD, prompts, routing, guardrail inspection, reload, backups — `require_platform_admin`); keys are re-homed only by platform admins (tenant admins' creates are forced into their own tenant, a mismatching explicit `tenant_id` is a 400). Auth fails closed: a dangling `tenant_id` (corrupt state) or `is_active: false` (the platform admin's kill switch) rejects every member key with 401 — a disabled tenant's keys are never served as unrestricted platform keys. Spend accounting writes both rows (key and tenant) in separate transactions; rate limits are two windows (the key's own and the shared `tenant:{id}` one, TPM included); budget alerts fire per level on either, with `kind: "key" | "tenant"` in the payload and fired levels persisted on the matching row. Data-plane responses carry `X-Gateway-Tenant` (the slug); log rows and the usage export carry `tenant_id` (NULL = platform key). Cache identity is tenant-salted: the exact/semantic chat caches fold in a **response-policy token** (the tenant plus every response-capable guardrail active for the caller, so a key's `disabled_guardrails` or a scoped guardrail splits entries) — a hit skips response guardrails, so callers share entries only when the policy that shaped them matches; the embeddings cache is salted by tenant alone (no guardrails run there).
- **Usage export** — `GET /admin/usage/export` is a billing/chargeback extract over the request log: rows in `[since, until]` (ISO-8601, naive timestamps read as UTC, default the last 30 days), filterable by `key_name`/`model`/`status`. CSV (stable column order, attachment download) or JSON (rows + a `summary` with totals and by-status/by-key/by-model aggregates, most-expensive-first). Mirrored shadow calls are excluded by default — they never cost the client anything — and included flagged with `include_shadow=true`; cache-hit rows appear with their cached token counts and 0 cost. The row list is capped by `limit` (default 100k, max 250k; the `truncated` flag says so) while aggregates always cover the whole window. The admin UI's "Export usage (CSV)" button downloads the selected window.
- **Circuit breakers** are per-process by default (each worker/replica trips and recovers on its own evidence); `routing.breaker.backend: redis` shares the state so one replica's trip skips the deployment fleet-wide, with `/metrics` scrapes and admin views re-syncing from shared state. If Redis is down, the breaker fails *open* (routes anyway) — protection, not correctness. For streams, only failures *before the first chunk* trip the breaker: once a stream is committed, later upstream errors surface to the client per the failover contract.
- **Admin UI** — a read-mostly operations tool, not an audit console: it edits keys, resets breakers and expands log rows (including stored request/response bodies) on top of the JSON admin API. The shell itself needs no auth because it contains no data; the admin key is pasted into a login overlay, kept in the browser tab's `sessionStorage` and sent as a bearer token. The page is fully self-contained (no CDN, fonts or scripts fetched — enforced by a test) so it works in air-gapped deployments, at the cost of hand-rolled vanilla JS in one file.
- **Observability** — Prometheus metrics at `/metrics` (scraping is the primary path; an optional bridge mirrors the series into OTLP), OpenTelemetry tracing with head-based sampling in-process and tail-based sampling at the collector (`deploy/`), JSON stdout logs + an OTel logs pipeline with trace-context stamping, and DB log rows carrying `trace_id`/`span_id` for trace ↔ log correlation. A full self-hosted stack (collector, Jaeger, Loki, Prometheus, Grafana dashboard + alert rules) ships in `deploy/`. Remaining gaps: no Alertmanager wiring for the Prometheus rules (budget alerts go via webhook instead), and per-key metrics exist only as DB aggregates, not Prometheus series.
- **Native inbound formats are translated, not passed through** — the gateway normalizes Anthropic/Gemini requests to its internal OpenAI shape and back. Fidelity notes: Anthropic `top_k` is dropped (no OpenAI equivalent); Gemini `candidateCount > 1` is rejected (one choice served); Gemini function calls carry no ids, so tool-call ids are synthesized from the function name (`call_<name>`); Gemini's JSON-array streaming variant is unsupported (`:streamGenerateContent` always answers SSE, which is what the SDKs request); on Anthropic streams `message_start` reports `input_tokens: 0` (real token counts arrive with the terminal `message_delta`, since the gateway only learns usage from the final upstream chunk); request logs store the normalized internal request, not the client's original bytes. Native endpoints can still hit the same cache entries as OpenAI clients — the translation is deterministic and the cache key covers only the normalized semantic fields.
- **The Responses bridge is stateless** — `POST /v1/responses` translates each call to a chat completion: `previous_response_id` (conversation state), `background` mode, server-side tools (`web_search`, `file_search`, `code_interpreter`, `computer_use`, `mcp`) and reasoning-model outputs are out of scope and rejected with clear 400s; `store`/`truncation`/`include` are accepted no-ops. Agents must send the full conversation each turn (the Agents SDK does). Reasoning items in `input` are dropped; framework-visible behavior otherwise matches the Responses API.

## Endpoint compatibility notes

- Errors use the OpenAI envelope by default: `{"error": {"message", "type", "param", "code"}}` — the native Anthropic/Gemini endpoints answer with their own envelopes instead (`{"type": "error", "error": {...}}` / `{"error": {"code", "message", "status"}}`)
- `response_format` is honored where the routed provider can honor it: OpenAI passthrough takes any shape, Gemini maps `json_object` (`responseMimeType`) and `json_schema` (`responseSchema`), and Anthropic deployments **reject** it with `400 response_format_not_supported` (no JSON mode exists there) — the router first falls through to later deployments that can honor it, so a model with an OpenAI/Gemini fallback serves the request from there
- Guardrail-blocked requests → `400` with `code: "content_policy_violation"`
- Guardrail-blocked responses → `200` with `finish_reason: "content_filter"` (OpenAI semantics)
- Rate limit → `429` `rate_limit_exceeded`; budget → `429` `insufficient_quota`; unknown/unauthorized model → `404` `model_not_found`; endpoint-scope / IP violation → `403` `endpoint_not_allowed` / `ip_not_allowed`
