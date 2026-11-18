"""GuardrailManager: loads, registers and executes guardrails per phase."""

from __future__ import annotations

import fnmatch
import hashlib
import importlib
import json
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from ..config import GuardrailConfig
from ..errors import GuardrailBlocked
from ..observability.metrics import GUARDRAIL_BLOCKS, GUARDRAIL_OBSERVED
from ..schemas import ChatCompletionRequest
from .base import Guardrail, GuardrailResult
from .builtins import BUILTIN_GUARDRAILS

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import RequestContext


def _load_class(class_path: str) -> type[Guardrail]:
    """Load a custom guardrail class from a dotted 'pkg.module:ClassName' path."""
    module_path, _, class_name = class_path.partition(":")
    if not module_path or not class_name:
        raise ValueError(
            f"Invalid guardrail class path '{class_path}' (expected 'pkg.module:ClassName')"
        )
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, Guardrail)):
        raise TypeError(f"{class_path} is not a Guardrail subclass")
    return cls


class StreamRewriteState:
    """Per-stream state for mid-stream rewriting guardrails.

    One instance per streamed request, owned by the pipeline's stream
    loop: each guardrail's pending buffer (text held back because it
    might still change) and a count of delta groups it modified.
    """

    __slots__ = ("guardrails", "pending", "modified_counts")

    def __init__(self, guardrails: list[Guardrail]) -> None:
        self.guardrails = guardrails
        self.pending: dict[str, str] = {g.name: "" for g in guardrails}
        self.modified_counts: dict[str, int] = {g.name: 0 for g in guardrails}


class GuardrailManager:
    def __init__(self, tracer: trace.Tracer | None = None) -> None:
        self._guardrails: list[Guardrail] = []
        # OTel API is a no-op until a provider is installed, so this is safe
        # to construct unconditionally.
        self._tracer = tracer or trace.get_tracer("gateway.guardrails")

    # -- registration --------------------------------------------------------

    def register(self, guardrail: Guardrail) -> None:
        self._guardrails.append(guardrail)

    @classmethod
    def from_config(
        cls, configs: list[GuardrailConfig], tracer: trace.Tracer | None = None
    ) -> "GuardrailManager":
        manager = cls(tracer=tracer)
        for cfg in configs:
            if not cfg.enabled:
                continue
            if cfg.class_path:
                guardrail_cls = _load_class(cfg.class_path)
            elif cfg.name in BUILTIN_GUARDRAILS:
                guardrail_cls = BUILTIN_GUARDRAILS[cfg.name]
            else:
                raise ValueError(
                    f"Unknown guardrail '{cfg.name}'. Use a built-in name or a "
                    f"'class' entry pointing at your own Guardrail subclass."
                )
            manager.register(guardrail_cls(cfg.params))
            # Mode and scope come from config, not the constructor, so custom
            # guardrails keep their documented (params) signature.
            manager._guardrails[-1].mode = cfg.mode
            manager._guardrails[-1].scope = cfg.scope
        return manager

    # -- introspection -------------------------------------------------------

    @property
    def guardrails(self) -> list[Guardrail]:
        return list(self._guardrails)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": g.name,
                "description": g.description,
                "params": g.params,
                "mode": g.mode,
                "scope": g.scope.model_dump() if getattr(g, "scope", None) else None,
            }
            for g in self._guardrails
        ]

    def response_policy_token(self, ctx: "RequestContext") -> str:
        """A stable fingerprint of the response policy applying to this
        request: every *response-capable* guardrail that is active here
        (name + params) plus the tenant. It salts the exact and semantic
        cache keys — see the pipeline's cache lookup.

        Why it exists: a cache hit replays the stored body without
        re-running response-phase guardrails, so two callers may share
        an entry only when the policy that shapes responses is the same.
        Applicability here covers everything that can differ: key-level
        ``disabled_guardrails``, and key/model/route scoping (a
        redactor scoped to other keys never applied to the stored
        body). Observe-mode guardrails never modify anything, so they
        are excluded. The tenant rides along for data isolation — two
        tenants never read each other's cached responses, even under
        identical policies. The key id is deliberately NOT part of it:
        two keys with the same response policy sharing entries is the
        point of the cache.
        """
        parts: list[list[Any]] = []
        for g in self._active(ctx):
            if g.mode == "observe":
                continue  # observe never changes a response
            can_touch_response = (
                type(g).check_response is not Guardrail.check_response
                or type(g).check_stream_delta is not Guardrail.check_stream_delta
                or bool(getattr(g, "supports_stream_rewrite", False))
            )
            if not can_touch_response:
                continue
            parts.append([g.name, g.params])
        payload_dict: dict[str, Any] = {
            "tenant_id": ctx.auth.tenant_id,
            "response_guardrails": parts,
        }
        # Data-residency requirement (tier 28): an entry produced by an
        # unrestricted call may have been generated on a deployment a
        # restricted caller may not use — serving it would hand them
        # content of non-compliant origin. Residency classes therefore
        # never share entries. Only added when a requirement exists, so
        # unrestricted callers keep their exact pre-tier-28 tokens
        # (existing cache entries stay valid).
        residencies = ctx.auth.effective_residencies()
        if residencies is not None:
            payload_dict["required_residencies"] = sorted(residencies)
        payload = json.dumps(payload_dict, sort_keys=True, default=repr)
        return hashlib.sha256(payload.encode()).hexdigest()

    # -- scoping ----------------------------------------------------------------

    def _in_scope(self, guardrail: Guardrail, ctx: "RequestContext") -> bool:
        """Config scope: does this guardrail apply to this request?

        Patterns are fnmatch globs. ``exclude_*`` wins over ``keys`` /
        ``models`` / ``routes``; a request must match every present field.
        Key names are the human labels from /admin/keys (the admin key is
        "admin"); models are public names; routes are the inbound API
        surface ("chat" = /v1/chat/completions, "responses" = /v1/responses,
        "messages" = the native Anthropic format, "gemini" = the native
        Gemini format). Guardrails registered programmatically
        (no scope set) apply everywhere, unchanged.
        """
        scope = getattr(guardrail, "scope", None)
        if scope is None:
            return True
        key_name = ctx.auth.key_name or ""
        if scope.exclude_keys and any(
            fnmatch.fnmatch(key_name, p) for p in scope.exclude_keys
        ):
            return False
        if scope.keys and not any(fnmatch.fnmatch(key_name, p) for p in scope.keys):
            return False
        model = ctx.model or ""
        if scope.exclude_models and any(
            fnmatch.fnmatch(model, p) for p in scope.exclude_models
        ):
            return False
        if scope.models and not any(fnmatch.fnmatch(model, p) for p in scope.models):
            return False
        # Route scoping: a request without a route attribute (programmatic
        # pipeline calls) matches no route pattern — scoped-to-route
        # guardrails do not run for it.
        if scope.exclude_routes and ctx.route is not None and any(
            fnmatch.fnmatch(ctx.route, p) for p in scope.exclude_routes
        ):
            return False
        if scope.routes and (
            ctx.route is None
            or not any(fnmatch.fnmatch(ctx.route, p) for p in scope.routes)
        ):
            return False
        return True

    def _active(self, ctx: "RequestContext") -> list[Guardrail]:
        """Guardrails that apply to this request: scope filter + the key's
        runtime override (``disabled_guardrails`` on the virtual key row,
        settable via the admin API without a restart — it wins over config
        scope). Out-of-scope guardrails are skipped entirely: no verdict,
        no log entry, no metric."""
        disabled = set(ctx.auth.disabled_guardrails or [])
        return [
            g
            for g in self._guardrails
            if g.name not in disabled and self._in_scope(g, ctx)
        ]

    # -- phase execution -------------------------------------------------------

    def _record(
        self, ctx: "RequestContext", guardrail: Guardrail, phase: str, result: GuardrailResult
    ) -> None:
        observed = guardrail.mode == "observe"
        action = result.action.value
        ctx.guardrail_actions.append(
            {
                "guardrail": guardrail.name,
                "phase": phase,
                "action": action,
                "reason": result.reason,
                "details": result.details or None,
                "mode": guardrail.mode,
            }
        )
        # Surface verdicts as events on the current span: the phase span for
        # request/response checks, the gateway.chat span for stream checks.
        # Replay-harness executions (ctx.dry_run) record verdicts on the
        # ctx — the replay result is built from them — but must NOT touch
        # the enforcement counters: a would-be verdict from POST
        # /admin/replay is not live traffic, and gateway_guardrail_blocks_
        # total drives alerts.
        span = trace.get_current_span()
        if observed:
            if action in ("block", "modify"):
                if not ctx.dry_run:
                    GUARDRAIL_OBSERVED.labels(
                        guardrail=guardrail.name, phase=phase, action=action
                    ).inc()
                span.add_event(
                    "gateway.guardrail.observed",
                    {
                        "guardrail": guardrail.name,
                        "phase": phase,
                        "would": action,
                        "reason": result.reason or action,
                    },
                )
            return
        if action == "block":
            if not ctx.dry_run:
                GUARDRAIL_BLOCKS.labels(guardrail=guardrail.name, phase=phase).inc()
            span.add_event(
                "gateway.guardrail.blocked",
                {
                    "guardrail": guardrail.name,
                    "phase": phase,
                    "reason": result.reason or "blocked",
                },
            )
        elif action == "modify":
            span.add_event(
                "gateway.guardrail.modified", {"guardrail": guardrail.name, "phase": phase}
            )

    async def run_request_phase(
        self, request: ChatCompletionRequest, ctx: "RequestContext"
    ) -> ChatCompletionRequest:
        """Run all request-phase guardrails; returns the (possibly modified) request.

        Raises GuardrailBlocked if any enforcing guardrail blocks. Guardrails
        in observe mode never block or modify — their verdicts are only
        logged/counted, and later guardrails still run.

        Modifications chain: each guardrail sees the previous one's output
        (messages, model and fields all compose — one ``model_copy`` per
        guardrail). A model redirect (``modified_model``) is *not* resolved
        through aliases and is not allowlist-checked here — the pipeline
        re-checks permissions after this phase, fail closed.
        """
        guardrails = self._active(ctx)
        if not guardrails:
            return request
        with self._tracer.start_as_current_span("gateway.guardrails.request"):
            current = request
            for guardrail in guardrails:
                result = await guardrail.check_request(current, ctx)
                if result is None:
                    continue
                self._record(ctx, guardrail, "request", result)
                if guardrail.mode == "observe":
                    continue
                if result.action.value == "block":
                    raise GuardrailBlocked(guardrail.name, result.reason or "blocked")
                if result.action.value == "modify":
                    update: dict[str, Any] = {}
                    if result.modified_messages is not None:
                        update["messages"] = result.modified_messages
                    if result.modified_model is not None:
                        update["model"] = result.modified_model
                    if result.modified_request_fields:
                        # Overlay only real request fields; anything else
                        # would silently vanish in model_dump anyway.
                        update.update(
                            {
                                k: v
                                for k, v in result.modified_request_fields.items()
                                if k in ChatCompletionRequest.model_fields
                            }
                        )
                    if update:
                        current = current.model_copy(update=update)
            return current

    async def run_response_phase(
        self,
        request: ChatCompletionRequest,
        response: dict[str, Any],
        ctx: "RequestContext",
    ) -> dict[str, Any]:
        """Run all response-phase guardrails; returns the (possibly modified) response.

        Raises GuardrailBlocked if any enforcing guardrail blocks. Guardrails
        in observe mode never block or modify.
        """
        guardrails = self._active(ctx)
        if not guardrails:
            return response
        with self._tracer.start_as_current_span("gateway.guardrails.response"):
            current = response
            for guardrail in guardrails:
                result = await guardrail.check_response(request, current, ctx)
                if result is None:
                    continue
                self._record(ctx, guardrail, "response", result)
                if guardrail.mode == "observe":
                    continue
                if result.action.value == "block":
                    raise GuardrailBlocked(guardrail.name, result.reason or "blocked")
                if result.action.value == "modify" and result.modified_response is not None:
                    current = result.modified_response
            return current

    async def run_stream_phase(
        self,
        request: ChatCompletionRequest,
        accumulated_text: str,
        delta_text: str,
        ctx: "RequestContext",
    ) -> None:
        """Run stream-phase guardrails over a chunk. Raises GuardrailBlocked to abort.

        No span is created per chunk (that would explode cardinality); block
        verdicts are recorded as events on the enclosing gateway.chat span.
        Guardrails in observe mode never abort the stream.

        ``accumulated_text`` is the *source* text from the provider — even
        when stream rewriting is active, block-style checks see what the
        model actually said.
        """
        for guardrail in self._active(ctx):
            result = await guardrail.check_stream_delta(
                request, accumulated_text, delta_text, ctx
            )
            if result is None:
                continue
            self._record(ctx, guardrail, "stream", result)
            if guardrail.mode == "observe":
                continue
            if result.action.value == "block":
                raise GuardrailBlocked(guardrail.name, result.reason or "blocked")

    # -- stream rewriting -------------------------------------------------------

    def new_stream_rewrite_state(
        self, request: ChatCompletionRequest, ctx: "RequestContext"
    ) -> StreamRewriteState | None:
        """Per-stream state for rewriting guardrails, or None when no active
        guardrail rewrites streams (the common case — zero overhead then).
        Owned by the pipeline's stream loop, one per request."""
        rewriters = [g for g in self._active(ctx) if g.supports_stream_rewrite]
        if not rewriters:
            return None
        return StreamRewriteState(rewriters)

    async def run_stream_rewrite(
        self,
        request: ChatCompletionRequest,
        ctx: "RequestContext",
        state: StreamRewriteState,
        delta_text: str,
        final: bool = False,
    ) -> str:
        """Chain rewriting guardrails over new stream text; returns what to
        emit now.

        Each guardrail receives its own pending buffer plus the output of
        the previous guardrail, and returns (emit_now, keep_pending).
        In observe mode a rewriter still runs (its would-be modifications
        are counted and reported at the end) but its output is discarded —
        the client sees the unrewritten text.
        """
        text = delta_text
        for guardrail in state.guardrails:
            incoming = state.pending[guardrail.name] + text
            emit, pending = await guardrail.rewrite_stream_text(incoming, final=final)
            if guardrail.mode == "observe":
                if emit + pending != incoming:
                    state.modified_counts[guardrail.name] += 1
                continue  # observe: discard the output, keep text untouched
            state.pending[guardrail.name] = pending
            if emit + pending != incoming:
                state.modified_counts[guardrail.name] += 1
            text = emit
        return text

    def record_stream_rewrite(
        self, ctx: "RequestContext", state: StreamRewriteState
    ) -> None:
        """Record one guardrail_actions entry per rewriter that actually
        changed the streamed text (once, at the end of the stream — per-delta
        entries would be noise)."""
        span = trace.get_current_span()
        for guardrail in state.guardrails:
            count = state.modified_counts[guardrail.name]
            if not count:
                continue
            observed = guardrail.mode == "observe"
            ctx.guardrail_actions.append(
                {
                    "guardrail": guardrail.name,
                    "phase": "stream",
                    "action": "modify",
                    "reason": f"rewrote streamed output ({count} delta group(s))",
                    "details": {"modifications": count},
                    "mode": guardrail.mode,
                }
            )
            if observed:
                GUARDRAIL_OBSERVED.labels(
                    guardrail=guardrail.name, phase="stream", action="modify"
                ).inc()
            span.add_event(
                "gateway.guardrail.observed" if observed else "gateway.guardrail.modified",
                {"guardrail": guardrail.name, "phase": "stream"},
            )
