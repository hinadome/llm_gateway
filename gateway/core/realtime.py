"""Realtime (WebSocket) sessions — the stateful fifth inbound surface.

HTTP surfaces are one request, one response, one policy pass. A
realtime session is a long-lived bidirectional event stream: the
client connects once (``WS /v1/realtime?model=...``), and the gateway
holds an upstream connection open for the session's life — relaying
client events up and model events down, with the pipeline's policy
applied at the points where content actually moves.

What runs ONCE per session (the open gates — see
``GatewayPipeline.handle_realtime``):

- model permission, endpoint scoping ("realtime"), rate limit (one
  RPM hit at open; tokens accrue per response) and the budget check;
- a concurrency slot (``max_concurrency`` counts SESSIONS for
  realtime callers — a session occupies upstream capacity for its
  whole life, exactly like an in-flight stream);
- routing: the candidate ladder (residency-filtered, breaker-filtered)
  walked at CONNECT time only — a long-lived session cannot be
  retried mid-stream, so realtime fails over between handshakes and
  then stays put;
- the upstream handshake authenticates with the same credential the
  HTTP paths would use, BYOK vault included (a keyed tenant's session
  rides THEIR provider account), in the provider's own header shape
  (``adapter.ws_headers``).

What runs PER EVENT (the relay below):

- ``response.create`` — the request phase of guardrails, over the
  create event's input (instructions + items, or the shadowed
  conversation when the create forks from implied state). BLOCK
  becomes an OpenAI-shaped realtime error event and the create never
  reaches the upstream — the session lives on, the response does not.
  Modified messages are written back into the forwarded event.
- ``response.output_text.delta`` — block-style stream guardrails see
  the SOURCE text; rewriting guardrails (the same held-back protocol
  the SSE path uses) see what the client receives, with the final
  flush at ``output_text.done`` — so nothing a rewriter wants masked
  ever reaches the client, no matter how the provider chunked it.
  A mid-response stream BLOCK sends the error event, forwards
  ``response.cancel`` upstream and suppresses the rest of that
  response (its ``response.done`` still bills — the provider ran).
- ``response.done`` — usage accounting: tokens, TPM, cost, spend,
  budgets and the log row all see realtime traffic exactly like HTTP.

Text is the guarded surface (input_text items, instructions and
output_text deltas). Audio frames pass through — bytes are never
rewritten, mirroring the SSE policy for audio output.

Deliberately NOT on this surface (documented in the README): the
exact/semantic response caches (a session is not a replayable
request), idempotency, runaway loop detection (no single request to
fingerprint — a follow-up could hash response.create payloads), and
X-Gateway-* response headers (the WS handshake response is not
diagnostic the way HTTP is; observability flows through the request
log, the metrics and the span).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..errors import GatewayError, GuardrailBlocked
from ..schemas import ChatCompletionRequest, Message
from ..observability import metrics

if TYPE_CHECKING:  # pragma: no cover
    from .context import AuthContext, RequestContext
    from .pipeline import GatewayPipeline

logger = logging.getLogger("gateway.realtime")


def _event_response_id(event: dict) -> str | None:
    """The response an event belongs to (deltas carry ``response_id``,
    completion events nest it under ``response``)."""
    rid = event.get("response_id")
    if isinstance(rid, str):
        return rid
    response = event.get("response")
    if isinstance(response, dict):
        rid = response.get("id")
        if isinstance(rid, str):
            return rid
    return None


def _item_text(item: dict) -> tuple[str | None, str | None]:
    """(item_id, text) for a conversation/message item — the text the
    guardrails need to see; audio-only or non-message items yield no
    text (they are the media surface, guarded as nothing)."""
    if not isinstance(item, dict) or item.get("type") != "message":
        return None, None
    role = item.get("role") if isinstance(item.get("role"), str) else None
    texts = []
    for part in item.get("content") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    item_id = item.get("id") if isinstance(item.get("id"), str) else None
    text = "\n".join(t for t in texts if t)
    return item_id or None, (f"{role}\u0001{text}" if role and text else (text or None))


class RealtimeRelay:
    """Bidirectional event relay for one live session.

    Constructed by ``GatewayPipeline.handle_realtime`` AFTER the open
    gates passed and the upstream handshake succeeded; ``run()`` drives
    both pumps until either side disconnects. All state is per-session
    and per-response; nothing here is shared across sessions.
    """

    def __init__(
        self,
        pipeline: "GatewayPipeline",
        ctx: "RequestContext",
        ws: WebSocket,
        upstream: Any,
        native_model: str,
    ) -> None:
        self.pipeline = pipeline
        self.ctx = ctx
        self.ws = ws
        self.upstream = upstream
        self.native_model = native_model
        # The guardrail API is request-shaped; realtime synthesizes a
        # per-session carrier for the stream phase (the request phase
        # builds a fresh one per response.create from the actual input).
        self._gr_request = ChatCompletionRequest(model=ctx.model, messages=[])
        # Shadowed conversation: text items the guardrails need for
        # context when a response.create implies the conversation
        # state instead of carrying its input. (role, text) pairs;
        # ids tracked so deletes are honored when the protocol has them.
        self._conversation: list[tuple[str | None, Message]] = []
        # Per-response relay state.
        self._rewrite_state: Any = None
        self._source_text = ""  # what the model said (this response)
        self._emitted_text = ""  # what the client saw (after rewriting)
        self._response_open = False  # a response.create is being served
        self._suppressed: set[str] = set()  # stream-blocked responses
        # Session accounting (sums across responses — see _account).
        self._prompt_total = 0
        self._completion_total = 0
        self._cost_total = 0.0
        self._first_token_at: float | None = None
        self._events_relayed = 0
        self._responses_served = 0

    # -- entry point ------------------------------------------------------

    async def run(self) -> None:
        """Drive both pumps until either side disconnects. Returns
        normally on any disconnect; the pipeline's finally-block does
        the teardown (log row, metrics, slot release)."""
        up_task = asyncio.create_task(self._pump_upstream())
        client_task = asyncio.create_task(self._pump_client())
        done, pending = await asyncio.wait(
            {up_task, client_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # A pump that died with an exception (not a clean disconnect)
        # marks the session failed so the log row says so.
        for task in done:
            exc = task.exception()
            if exc is not None:
                self.ctx.status = "error"
                self.ctx.error_message = str(exc) or exc.__class__.__name__
        if not self._events_relayed and self.ctx.status == "success":
            # Connected, gated, routed — and the upstream said nothing
            # before closing. Not a client-facing failure, but not a
            # served session either; the row should say what happened.
            self.ctx.status = "error"
            self.ctx.error_message = "upstream closed the session before any event"

    # -- client -> upstream -------------------------------------------------

    async def _pump_client(self) -> None:
        try:
            while True:
                raw = await self.ws.receive_text()
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict) or not isinstance(
                        event.get("type"), str
                    ):
                        raise ValueError("event must be a JSON object with a 'type'")
                except ValueError as exc:
                    await self._send_error(
                        "invalid_request_error",
                        f"Malformed realtime event: {exc}",
                        code="invalid_event",
                    )
                    continue
                forward = await self._client_event(event)
                if forward is None:
                    continue
                await self.upstream.send(json.dumps(forward))
        except WebSocketDisconnect:
            return
        except GatewayError as exc:
            # A gate re-checked mid-session (the budget across
            # responses) is session-fatal: the error rides the event
            # stream in the OpenAI shape, then the socket closes 1008
            # — the same accept-first rejection the open gates use.
            # The row records why (the generic path below is for
            # transport faults, which carry no client-facing story).
            self.ctx.status = "error"
            self.ctx.error_message = exc.message
            await self._send_error(exc.error_type, exc.message, code=exc.code)
            with suppress(Exception):
                await self.ws.close(code=1008, reason=(exc.message or "rejected")[:120])
            return
        except Exception as exc:  # send on a closed socket, cancelled, ...
            if self.ctx.status == "success":
                self.ctx.status = "error"
                self.ctx.error_message = str(exc) or exc.__class__.__name__
            return

    async def _client_event(self, event: dict) -> dict | None:
        """Inspect one client event; returns the (possibly rewritten)
        event to forward, or None to swallow it locally."""
        etype = event.get("type")
        if etype == "response.create":
            return await self._on_response_create(event)
        if etype == "conversation.item.create":
            item = event.get("item")
            if isinstance(item, dict):
                item_id, tagged = _item_text(item)
                if tagged:
                    role, _, text = tagged.partition("\u0001")
                    self._conversation.append(
                        (item_id, Message(role=role or "user", content=text))
                    )
            return event
        if etype == "conversation.item.delete":
            item_id = event.get("item_id")
            if isinstance(item_id, str):
                self._conversation = [
                    (i, m) for i, m in self._conversation if i != item_id
                ]
            return event
        return event

    async def _on_response_create(self, event: dict) -> dict | None:
        """The request-phase gate: guardrails see the input this
        response will run on — the create event's own input when it
        carries one (fork semantics), the shadowed conversation when it
        doesn't. BLOCK → error event, the create never goes upstream;
        the session survives, the response does not."""
        response = event.get("response")
        if not isinstance(response, dict):
            response = {}
        messages = self._create_messages(response)
        if not messages:
            # Nothing textual (audio-only input): the media surface is
            # guarded as nothing — forward untouched.
            self._start_response_relay(event)
            return event

        # Long sessions burn budget across responses: re-check before
        # each one, against the snapshot updated with this session's
        # own spend so far (see _account).
        self.pipeline._check_budget(self.ctx)

        request = ChatCompletionRequest(model=self.ctx.model, messages=messages)
        try:
            modified = await self.pipeline.guardrails.run_request_phase(
                request, self.ctx
            )
        except GuardrailBlocked as exc:
            await self._send_error(
                "invalid_request_error",
                f"Blocked by guardrail '{exc.guardrail}': {exc.reason}",
                code="guardrail_blocked",
            )
            return None
        if modified.messages == messages:
            self._start_response_relay(event)
            return event
        # A guardrail rewrote the input: write it back into the create
        # event (instructions from the system message, the rest as
        # message items) — the same MODIFY semantics the HTTP surfaces
        # give guardrails, adapted to realtime's event shape.
        rewritten = dict(event)
        new_response = dict(response)
        instructions = None
        items = []
        for message in modified.messages:
            content = message.content if isinstance(message.content, str) else None
            if content is None:
                continue
            if message.role == "system" and instructions is None:
                instructions = content
                continue
            part_type = "text" if message.role == "assistant" else "input_text"
            items.append(
                {
                    "type": "message",
                    "role": message.role,
                    "content": [{"type": part_type, "text": content}],
                }
            )
        if instructions is not None:
            new_response["instructions"] = instructions
        new_response["input"] = items
        rewritten["response"] = new_response
        self._start_response_relay(event)
        return rewritten

    def _create_messages(self, response: dict) -> list[Message]:
        """Messages for the request phase: instructions + the create's
        input when it carries one (fork semantics), otherwise the
        shadowed conversation."""
        messages: list[Message] = []
        instructions = response.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            messages.append(Message(role="system", content=instructions))
        inp = response.get("input")
        if isinstance(inp, str):
            if inp.strip():
                messages.append(Message(role="user", content=inp))
            return messages
        if isinstance(inp, list):
            for item in inp:
                _, tagged = _item_text(item)
                if tagged:
                    role, _, text = tagged.partition("\u0001")
                    messages.append(Message(role=role or "user", content=text))
            return messages
        # No input on the create: the response runs on the conversation
        # state — which the relay shadows for exactly this check.
        for _, message in self._conversation:
            messages.append(message)
        return messages

    def _start_response_relay(self, event: dict) -> None:
        """Reset per-response state (stream guardrails + rewriting see
        one response at a time, like one SSE stream)."""
        self._response_open = True
        self._source_text = ""
        self._emitted_text = ""
        self._rewrite_state = self.pipeline.guardrails.new_stream_rewrite_state(
            self._gr_request, self.ctx
        )

    # -- upstream -> client -------------------------------------------------

    async def _pump_upstream(self) -> None:
        try:
            async for message in self.upstream:
                if isinstance(message, bytes):
                    # Binary frames pass through un-inspected (audio).
                    try:
                        await self.ws.send_bytes(message)
                    except Exception:
                        return
                    continue
                try:
                    event = json.loads(message)
                    if not isinstance(event, dict):
                        raise ValueError("event is not a JSON object")
                except ValueError:
                    # A non-JSON frame from the upstream is not ours to
                    # fix: pass it through as text (the client and the
                    # provider speak the same protocol dialect).
                    try:
                        await self.ws.send_text(message)
                    except Exception:
                        return
                    continue
                try:
                    out = await self._upstream_event(event)
                except GuardrailBlocked as exc:
                    # Safety net: block verdicts are handled where the
                    # response_id is known (_on_text_delta); anything
                    # that still escapes gets the error event and the
                    # pump keeps relaying (the session survives).
                    await self._send_error(
                        "invalid_request_error",
                        f"Blocked by guardrail '{exc.guardrail}': {exc.reason}",
                        code="guardrail_blocked",
                    )
                    continue
                if out is None:
                    continue
                try:
                    await self.ws.send_text(json.dumps(out))
                except Exception:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.ctx.status == "success":
                self.ctx.status = "error"
                self.ctx.error_message = str(exc) or exc.__class__.__name__
            return

    async def _upstream_event(self, event: dict) -> dict | None:
        """Inspect one upstream event; returns the (possibly rewritten)
        event to relay, or None to suppress it."""
        etype = event.get("type")
        response_id = _event_response_id(event)
        # Everything belonging to a stream-blocked response is dropped
        # from the client's view (the error event was already sent).
        # The terminal response.done still bills — it is captured in
        # _account below before the suppression check.
        if etype == "response.done":
            await self._account(event)
            if response_id in self._suppressed:
                return None
            self._response_open = False
            self._rewrite_state = None
            return event
        if response_id in self._suppressed:
            return None

        if etype == "response.output_text.delta":
            return await self._on_text_delta(event)
        if etype == "response.output_text.done":
            return await self._on_text_done(event)
        if etype == "response.output_item.done":
            # The assistant's finished item joins the shadowed
            # conversation (guardrail context for the next create).
            item = event.get("item")
            if isinstance(item, dict):
                item_id, tagged = _item_text(item)
                if tagged:
                    role, _, text = tagged.partition("\u0001")
                    self._conversation.append(
                        (item_id, Message(role=role or "assistant", content=text))
                    )
            return event
        if etype == "conversation.item.deleted":
            item_id = event.get("item_id")
            if isinstance(item_id, str):
                self._conversation = [
                    (i, m) for i, m in self._conversation if i != item_id
                ]
            return event
        if etype == "error":
            # Upstream errors relay as-is (already the right shape) and
            # end any open response cleanly.
            self._response_open = False
            self._rewrite_state = None
            return event
        self._events_relayed += 1
        return event

    async def _on_text_delta(self, event: dict) -> dict | None:
        """The stream phase, realtime-shaped: block checks see the
        SOURCE text, rewriters change what the client receives, and
        the final flush rides output_text.done — the same held-back
        protocol the SSE path uses, delta for delta."""
        delta = event.get("delta")
        if not isinstance(delta, str) or not delta:
            return event
        if self._first_token_at is None:
            self._first_token_at = time.monotonic()
            elapsed = self._first_token_at - self.ctx.started_monotonic
            self.ctx.ttft_ms = int(elapsed * 1000)
            metrics.TTFT.labels(model=self.ctx.model).observe(elapsed)
        self._source_text += delta
        # Block verdicts are handled HERE, where the response_id is in
        # hand: error event to the client, response.cancel upstream,
        # and the rest of THAT response suppressed (its response.done
        # still bills — see _upstream_event). The session lives on.
        try:
            await self.pipeline.guardrails.run_stream_phase(
                self._gr_request, self._source_text, delta, self.ctx
            )
        except GuardrailBlocked as exc:
            rid = event.get("response_id")
            if isinstance(rid, str):
                self._suppressed.add(rid)
                with suppress(Exception):
                    await self.upstream.send(
                        json.dumps({"type": "response.cancel", "response_id": rid})
                    )
            await self._send_error(
                "invalid_request_error",
                f"Blocked by guardrail '{exc.guardrail}': {exc.reason}",
                code="guardrail_blocked",
            )
            return None
        emit = delta
        if self._rewrite_state is not None:
            emit = await self.pipeline.guardrails.run_stream_rewrite(
                self._gr_request, self.ctx, self._rewrite_state, delta, final=False
            )
        self._emitted_text += emit
        if not emit:
            # The rewriter is holding everything back — nothing to send
            # for this delta; the flush at done will carry it.
            return None
        self._events_relayed += 1
        return {**event, "delta": emit}

    async def _on_text_done(self, event: dict) -> dict | None:
        """Final flush: everything a rewriter held back is emitted now
        (final=True means the pending buffer must empty), and the done
        event's text reflects what the client actually saw."""
        if self._rewrite_state is not None:
            flush = await self.pipeline.guardrails.run_stream_rewrite(
                self._gr_request, self.ctx, self._rewrite_state, "", final=True
            )
            if flush:
                await self.ws.send_text(
                    json.dumps(
                        {
                            "type": "response.output_text.delta",
                            "response_id": event.get("response_id"),
                            "item_id": event.get("item_id"),
                            "output_index": event.get("output_index"),
                            "content_index": event.get("content_index"),
                            "delta": flush,
                        }
                    )
                )
                self._emitted_text += flush
        self._events_relayed += 1
        return {**event, "text": self._emitted_text}

    # -- accounting + errors -------------------------------------------------

    async def _account(self, done_event: dict) -> None:
        """Usage from response.done → the same accounting HTTP uses:
        tokens, TPM, cost, spend, per-key series. Totals are summed
        across the session's responses (one row per session)."""
        response = done_event.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return
        mapped = {
            "prompt_tokens": usage.get("input_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or 0,
            "total_tokens": usage.get("total_tokens") or 0,
        }
        details = usage.get("input_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens"):
            mapped["prompt_tokens_details"] = {"cached_tokens": details["cached_tokens"]}
        await self.pipeline._account_usage(self.ctx, mapped, self.native_model)
        # _account_usage records per-response values; the session row
        # wants sums. (cache_hit is never set on realtime rows, so the
        # early-return path inside cannot skip us.)
        response_cost = self.ctx.cost_usd or 0.0
        self._prompt_total += mapped["prompt_tokens"]
        self._completion_total += mapped["completion_tokens"]
        self._cost_total += response_cost
        self.ctx.prompt_tokens = self._prompt_total
        self.ctx.completion_tokens = self._completion_total
        self.ctx.cost_usd = self._cost_total
        # Keep the budget gate honest for the NEXT response.create on
        # this long-lived session: the snapshot predates this spend.
        if self.ctx.auth.key_id is not None:
            self.ctx.auth.spend_usd = (self.ctx.auth.spend_usd or 0) + response_cost
        if self.ctx.auth.tenant_id is not None:
            self.ctx.auth.tenant_spend_usd = (
                (self.ctx.auth.tenant_spend_usd or 0) + response_cost
            )
        self._responses_served += 1
        self.ctx.response_body = (
            {"text": self._emitted_text[:4000], "responses": self._responses_served}
            if self._emitted_text
            else None
        )

    async def _send_error(
        self, error_type: str, message: str, code: str | None = None
    ) -> None:
        """An OpenAI-shaped realtime error event (session-fatal errors
        close the socket too — see the API route; these are
        response-level: the session lives on)."""
        error: dict[str, Any] = {"type": error_type, "message": message[:500]}
        if code:
            error["code"] = code
        try:
            await self.ws.send_text(json.dumps({"type": "error", "error": error}))
        except Exception:
            pass  # the pump's error path owns the session teardown
