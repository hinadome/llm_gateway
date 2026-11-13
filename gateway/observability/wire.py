"""Wire capture: what actually crossed the HTTP boundary, sanitized.

Request-log rows carry the full wire picture — what the client sent
(headers + body), what the gateway sent the provider, and what the
provider answered (body + headers). Bodies were already captured; this
module adds the two header snapshots and the upstream request, all via
STRICT ALLOWLISTS (allow, never block: a new credential-bearing header
can never leak into stored rows by default — ``Authorization``,
``x-api-key``, ``x-goog-api-key`` and ``api-key`` are deliberately not
listed, and the upstream URL is stored as a path only because Gemini's
``?key=`` lives in the query string).

Three context-local slots, each populated at exactly one choke point:

- **client headers** — the app's request-id middleware copies the
  allowlisted subset of the inbound request headers into the request's
  context (and resets the upstream slots, so each request starts clean —
  a cache hit or an error before dispatch records None, not the previous
  request's wire data).
- **upstream request** — an httpx request hook on the shared provider
  client records method + path + body of what is about to go on the
  wire (the provider-native form: after model mapping, prompt injection,
  guardrail edits and provider adaptation). Each retry attempt
  overwrites the slot, so it holds the final attempt — the request the
  provider actually answered.
- **upstream response headers** — an httpx response hook on the same
  client. The request hook clears it first, so a failure with no
  response at all records None instead of a previous attempt's headers.

The pipeline snapshots the upstream slots into the RequestContext inside
``_log`` — in the request's own task, before the row is queued to the
background writer (``logging.background_write``), whose consumer would
otherwise read the slots from its own (empty) context. Shadow / A-B
mirror tasks inherit a copy of the parent's context at spawn time, so
they reset the upstream slots at start and read their own after their
own dispatch; the client-headers slot they inherit is correct as-is
(mirrors serve the same client request).

Allowlists are configurable (``logging.client_header_allowlist`` /
``logging.upstream_header_allowlist``); entries are lowercase header
names, a trailing ``*`` matches prefixes (e.g. ``x-ratelimit-*``).
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

import httpx

# Inbound client headers worth persisting: which SDK made the call
# (user-agent + the OpenAI SDKs' x-stainless-* telemetry) and
# client-side correlation ids. Credential headers deliberately absent.
DEFAULT_CLIENT_HEADER_ALLOWLIST: tuple[str, ...] = (
    "user-agent",
    "x-request-id",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-retry-count",
    "x-stainless-timeout",
    "x-stainless-read-timeout",
    "anthropic-version",
)

# Provider response headers worth persisting: the provider's own request
# ids (support-ticket correlation), rate-limit state and retry hints.
DEFAULT_UPSTREAM_HEADER_ALLOWLIST: tuple[str, ...] = (
    "x-request-id",
    "request-id",
    "retry-after",
    "openai-processing-ms",
    "openai-version",
    "x-ratelimit-*",
    "ratelimit-*",
)

# Cap on any single stored header value — rate-limit values and SDK
# names are short; anything absurd gets cut, not stored whole.
_MAX_VALUE_CHARS = 512

_client_headers: ContextVar[dict[str, str] | None] = ContextVar(
    "gateway_wire_client_headers", default=None
)
_upstream_request: ContextVar[dict[str, Any] | None] = ContextVar(
    "gateway_wire_upstream_request", default=None
)
_upstream_headers: ContextVar[dict[str, str] | None] = ContextVar(
    "gateway_wire_upstream_headers", default=None
)


def sanitize_headers(
    headers: Any, allowlist: tuple[str, ...] | list[str]
) -> dict[str, str]:
    """The allowlisted subset of ``headers`` — the only path header
    values take into stored rows.

    ``headers`` is anything with a case-insensitive ``get`` (Starlette /
    httpx Headers). Names are stored lowercase; wildcard entries
    (trailing ``*``) match prefixes, first value wins.
    """
    out: dict[str, str] = {}
    for entry in allowlist:
        name = entry.lower()
        if name.endswith("*"):
            prefix = name[:-1]
            for key in headers.keys():
                if key.lower().startswith(prefix) and key.lower() not in out:
                    out[key.lower()] = str(headers[key])[:_MAX_VALUE_CHARS]
        else:
            value = headers.get(name)
            if value is not None:
                out[name] = str(value)[:_MAX_VALUE_CHARS]
    return out


def set_client_headers(
    headers: Any, allowlist: tuple[str, ...] | list[str] | None = None
) -> None:
    """Middleware: snapshot the inbound request's allowlisted headers."""
    _client_headers.set(
        sanitize_headers(headers, tuple(allowlist or DEFAULT_CLIENT_HEADER_ALLOWLIST))
        or None
    )


def client_headers() -> dict[str, str] | None:
    return _client_headers.get()


def reset_upstream() -> None:
    """Clear both upstream slots. Called at request start (middleware) and
    at mirror-task start (which otherwise inherits the primary's values)."""
    _upstream_request.set(None)
    _upstream_headers.set(None)


def capture_upstream_request(request: httpx.Request, max_body_chars: int) -> None:
    """httpx request hook: record what is about to go on the wire.

    The body is stored parsed (it is JSON for every provider call except
    multipart uploads — image edits/variations and audio transcriptions
    — whose bodies are httpx *streaming* bodies: ``request.content``
    raises rather than reads them, so they record a note). Oversized
    bodies record a note too: the point is debugging, not mirroring
    megabytes into the database.
    """
    try:
        content = request.content
    except httpx.RequestNotRead:
        # Streaming (multipart) request body — reading it here would
        # consume the stream, so httpx refuses; record the transport
        # shape and move on. The body is still in request_body on the
        # row (the gateway's parsed view of what it handed over).
        content = None
    body: Any
    try:
        parsed = json.loads(content) if content else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    if parsed is not None and len(content) <= max_body_chars:
        body = parsed
    else:
        body = {
            "_note": "non-JSON or oversized body not stored",
            "bytes": len(content) if content is not None else None,
            "content_type": request.headers.get("content-type", ""),
        }
    _upstream_request.set(
        {
            "method": request.method,
            # path only, never the query — Gemini's ?key= lives there
            "path": request.url.path,
            "body": body,
        }
    )
    # A fresh attempt is starting: any previous headers are stale.
    _upstream_headers.set(None)


def capture_upstream_response(
    response: httpx.Response, allowlist: tuple[str, ...] | list[str] | None = None
) -> None:
    """httpx response hook: snapshot the provider's allowlisted headers."""
    _upstream_headers.set(
        sanitize_headers(
            response.headers, tuple(allowlist or DEFAULT_UPSTREAM_HEADER_ALLOWLIST)
        )
        or None
    )


def upstream_request() -> dict[str, Any] | None:
    return _upstream_request.get()


def upstream_headers() -> dict[str, str] | None:
    return _upstream_headers.get()
