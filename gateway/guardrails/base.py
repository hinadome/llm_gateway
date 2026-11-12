"""Guardrail framework.

A guardrail is a pluggable unit that inspects (and may modify or block)
traffic at well-defined phases of the request lifecycle:

* ``check_request``       — incoming request, before it reaches the provider.
                            May modify messages, the model (reject-and-
                            redirect) and arbitrary request fields.
* ``check_response``      — outgoing response (non-streaming), before it
                            reaches the client.
* ``check_stream_delta``  — outgoing stream chunks, while the response is
                            streaming to the client (block-only).
* ``rewrite_stream_text`` — outgoing stream text, *rewritten* as it streams
                            (opt-in per guardrail; PII never reaches the
                            client, no matter how it was chunked).

To add custom behaviour, subclass :class:`Guardrail`, implement the phases
you care about, and register it either in ``config.yaml`` via
``class: "pkg.module:ClassName"`` or programmatically through
:class:`~gateway.guardrails.manager.GuardrailManager.register`.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from ..schemas import ChatCompletionRequest, Message

if TYPE_CHECKING:  # pragma: no cover
    from ..config import GuardrailScope
    from ..core.context import RequestContext


class GuardrailAction(str, Enum):
    ALLOW = "allow"
    MODIFY = "modify"
    BLOCK = "block"


@dataclass
class GuardrailResult:
    """Verdict returned by a guardrail check.

    * ``action=ALLOW``  — pass through untouched.
    * ``action=MODIFY`` — replace messages / response with the modified copy.
    * ``action=BLOCK``  — stop the request with a 400 content-policy error.

    Request-phase MODIFY can go beyond messages: ``modified_model``
    redirects the request to another public model (reject-and-redirect —
    "this content is too expensive for gpt-4o, send it to gpt-4o-mini"),
    and ``modified_request_fields`` overlays arbitrary request fields
    (e.g. ``temperature``, ``tools``). All three compose: one
    ``model_copy(update=...)`` per guardrail, each guardrail seeing the
    previous one's output.
    """

    action: GuardrailAction = GuardrailAction.ALLOW
    reason: str | None = None
    modified_messages: list[Message] | None = None  # request phase
    modified_response: dict[str, Any] | None = None  # response phase
    # request phase: redirect the request to another public model. The
    # pipeline re-checks the key's model allowlist after the guardrail
    # phase (redirect must never bypass permissions) and routes to the
    # new model; an unknown target fails in the router with a clear 404.
    modified_model: str | None = None
    # request phase: overlay arbitrary ChatCompletionRequest fields
    # ("temperature", "tools", ...). Unknown field names are ignored.
    modified_request_fields: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def block(reason: str, **details: Any) -> "GuardrailResult":
        return GuardrailResult(action=GuardrailAction.BLOCK, reason=reason, details=details)

    @staticmethod
    def modified(
        messages: list[Message] | None = None,
        response: dict | None = None,
        reason: str | None = None,
        model: str | None = None,
        fields: dict[str, Any] | None = None,
        **details: Any,
    ) -> "GuardrailResult":
        return GuardrailResult(
            action=GuardrailAction.MODIFY,
            reason=reason,
            modified_messages=messages,
            modified_response=response,
            modified_model=model,
            modified_request_fields=fields,
            details=details,
        )

    @staticmethod
    def redirect(model: str, reason: str, **details: Any) -> "GuardrailResult":
        """Reject-and-redirect: serve this request on another public model
        instead of blocking it. The key's model allowlist still applies
        (re-checked by the pipeline after the guardrail phase, fail closed)."""
        return GuardrailResult(
            action=GuardrailAction.MODIFY,
            reason=reason,
            modified_model=model,
            details=details,
        )


class Guardrail(ABC):
    """Base class for all guardrails.

    Override only the phases you need; the defaults are no-ops. ``params``
    comes from ``config.yaml`` (`guardrails[].params`).
    """

    name: ClassVar[str] = "guardrail"
    description: ClassVar[str] = ""

    # Set by the GuardrailManager from GuardrailConfig.mode: "observe"
    # suppresses enforcement of this guardrail's block/modify verdicts —
    # they are logged and counted, but the request flows on untouched.
    # Subclasses never need to read it; the manager enforces it.
    mode: ClassVar[str] = "enforce"
    # Set by the GuardrailManager from GuardrailConfig.scope: limits which
    # keys/models this guardrail runs for. None (the default) = everywhere.
    # Subclasses never read it; the manager filters before calling phases.
    scope: ClassVar["GuardrailScope | None"] = None

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = params or {}

    # -- request phase (incoming) -------------------------------------------
    async def check_request(
        self, request: ChatCompletionRequest, ctx: "RequestContext"
    ) -> GuardrailResult | None:
        """Inspect the incoming request. Return None to skip this phase."""
        return None

    # -- response phase (outgoing, non-streaming) ---------------------------
    async def check_response(
        self,
        request: ChatCompletionRequest,
        response: dict[str, Any],
        ctx: "RequestContext",
    ) -> GuardrailResult | None:
        """Inspect the outgoing response. Return None to skip this phase."""
        return None

    # -- stream phase (outgoing, streaming) ----------------------------------
    async def check_stream_delta(
        self,
        request: ChatCompletionRequest,
        accumulated_text: str,
        delta_text: str,
        ctx: "RequestContext",
    ) -> GuardrailResult | None:
        """Inspect streamed output as it is produced.

        ``accumulated_text`` is everything sent so far; ``delta_text`` is the
        new chunk. Only ``BLOCK`` is honoured in this phase — to *rewrite*
        stream text, set ``supports_stream_rewrite`` and implement
        :meth:`rewrite_stream_text` instead (both hooks can coexist).
        """
        return None

    # -- stream rewriting (outgoing, streaming) -------------------------------
    # Opt in by setting supports_stream_rewrite = True (an instance
    # attribute works too — e.g. only when a param enables it). Rewriting
    # guardrails are chained: each one sees the previous one's output.
    supports_stream_rewrite: ClassVar[bool] = False

    async def rewrite_stream_text(self, text: str, *, final: bool = False) -> tuple[str, str]:
        """Rewrite streamed output text as it is produced.

        Receives the text not yet emitted (this guardrail's *pending*
        buffer plus the new delta, after earlier guardrails in the chain
        have run). Returns ``(emit_now, keep_pending)``:

        * ``emit_now``      — text to send to the client immediately;
        * ``keep_pending``  — text to hold back and re-examine when more
          text arrives (or at the final flush).

        Hold back only what might still change: a suffix that an upcoming
        chunk could complete into a match. ``final=True`` means no more
        text will arrive — everything must be returned in ``emit_now``
        (redact all matches, ``keep_pending`` empty). The default passes
        text through untouched.
        """
        return text, ""
