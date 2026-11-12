"""PII redaction guardrail.

Detects and masks common PII categories in the *incoming* request
(and optionally the *outgoing* response — including responses that
*stream*) using regex patterns:

* emails, phone numbers (US/EU-ish), SSNs, credit cards (Luhn-validated)
* API-key-shaped secrets (``sk-...``, ``AKIA...``, ``ghp_...``)
* IPv4 addresses

Streaming (``mask_in_stream``) uses a reserve heuristic: the last
``stream_reserve_chars`` characters (default 100) are held back each
delta, because a PII match may still grow as more text arrives (an email
split across chunks must not leak its first half). Held-back text flushes
automatically as it leaves the reserve zone, and everything flushes
redacted before the stream's finish chunk. Patterns that can grow by
more than the reserve will still be split — raise
``stream_reserve_chars`` for those.

This is a heuristic best-effort implementation. For strict compliance use a
custom guardrail backed by e.g. Microsoft Presidio or a cloud DLP service.
"""

from __future__ import annotations

import re
from typing import Any

from ...schemas import ChatCompletionRequest, Message
from ..base import Guardrail, GuardrailResult

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_API_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
_CREDIT_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", candidate)]
    if not (13 <= len(digits) <= 19):
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


class PIIRedactionGuardrail(Guardrail):
    name = "pii_redaction"
    description = "Mask emails, phones, SSNs, credit cards, IP addresses and API keys in prompts (and optionally responses)."

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.types: set[str] = set(self.params.get("types", ["email", "phone", "ssn", "credit_card", "api_key", "ipv4"]))
        self.mask_in_response: bool = bool(self.params.get("mask_in_response", False))
        self.mask_in_stream: bool = bool(self.params.get("mask_in_stream", False))
        self.stream_reserve_chars: int = int(self.params.get("stream_reserve_chars", 100))
        self.placeholder: str = self.params.get("placeholder", "[REDACTED:{TYPE}]")
        # Opt this instance into stream rewriting only when configured
        # (ClassVar default is False).
        self.supports_stream_rewrite = self.mask_in_stream

    # -- internals -----------------------------------------------------------

    def _patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        """(label, pattern) pairs for the enabled PII types, in the same
        fixed order _redact_text applies them."""
        pii_types = {
            "email": _EMAIL,
            "phone": _PHONE,
            "ssn": _SSN,
            "credit_card": _CREDIT_CARD,
            "api_key": _API_KEY,
            "ipv4": _IPV4,
        }
        order = ["email", "ssn", "credit_card", "phone", "api_key", "ipv4"]
        return [
            (t.upper(), pii_types[t]) for t in order if t in self.types and t in pii_types
        ]

    def _redact_text(self, text: str) -> tuple[str, dict[str, int]]:
        counts: dict[str, int] = {}
        pairs: list[tuple[str, re.Pattern[str]]] = [
            ("EMAIL", _EMAIL),
            ("SSN", _SSN),
            ("CREDIT_CARD", _CREDIT_CARD),
            ("PHONE", _PHONE),
            ("API_KEY", _API_KEY),
            ("IPV4", _IPV4),
        ]
        pii_types = {
            "email": "EMAIL",
            "phone": "PHONE",
            "ssn": "SSN",
            "credit_card": "CREDIT_CARD",
            "api_key": "API_KEY",
            "ipv4": "IPV4",
        }
        enabled = {label for key, label in pii_types.items() if key in self.types}

        def replace_for(label: str, pattern: re.Pattern[str], match: re.Match[str]) -> str:
            counts[label] = counts.get(label, 0) + 1
            return self.placeholder.format(TYPE=label)

        for label, pattern in pairs:
            if label not in enabled:
                continue
            if label == "CREDIT_CARD":

                def _cc_sub(m: re.Match[str]) -> str:
                    if _luhn_ok(m.group(0)):
                        return replace_for(label, pattern, m)
                    return m.group(0)

                text = pattern.sub(_cc_sub, text)
            else:
                text = pattern.sub(lambda m, lbl=label, pat=pattern: replace_for(lbl, pat, m), text)
        return text, counts

    def _redact_content(self, content: Any) -> tuple[Any, dict[str, int]]:
        total: dict[str, int] = {}
        if isinstance(content, str):
            new_text, counts = self._redact_text(content)
            return new_text, counts
        if isinstance(content, list):
            new_parts: list[Any] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    new_text, counts = self._redact_text(part["text"])
                    for k, v in counts.items():
                        total[k] = total.get(k, 0) + v
                    new_parts.append({**part, "text": new_text})
                else:
                    new_parts.append(part)
            return new_parts, total
        return content, total

    def _redact_messages(self, messages: list[Message]) -> tuple[list[Message], dict[str, int]]:
        total: dict[str, int] = {}
        new_messages: list[Message] = []
        for message in messages:
            new_content, counts = self._redact_content(message.content)
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v
            if new_content is message.content:
                new_messages.append(message)
            else:
                new_messages.append(message.model_copy(update={"content": new_content}))
        return new_messages, total

    # -- phases ----------------------------------------------------------------

    async def check_request(
        self, request: ChatCompletionRequest, ctx
    ) -> GuardrailResult | None:
        new_messages, counts = self._redact_messages(request.messages)
        if not counts:
            return None
        return GuardrailResult.modified(
            messages=new_messages,
            reason=f"redacted PII: {counts}",
            counts=counts,
        )

    async def check_response(
        self, request: ChatCompletionRequest, response: dict[str, Any], ctx
    ) -> GuardrailResult | None:
        if not self.mask_in_response:
            return None
        total: dict[str, int] = {}
        modified = False
        new_response: dict[str, Any] = dict(response)
        new_choices = []
        for choice in response.get("choices", []):
            choice = dict(choice)
            message = dict(choice.get("message") or {})
            content = message.get("content")
            if isinstance(content, str):
                new_text, counts = self._redact_text(content)
                if counts:
                    message["content"] = new_text
                    modified = True
                    for k, v in counts.items():
                        total[k] = total.get(k, 0) + v
            # Audio responses carry the words in message.audio.transcript —
            # redact that text too (the audio bytes themselves cannot be
            # rewritten; the transcript is what clients render).
            audio = message.get("audio")
            if isinstance(audio, dict) and audio.get("transcript"):
                new_transcript, counts = self._redact_text(str(audio["transcript"]))
                if counts:
                    message["audio"] = dict(audio, transcript=new_transcript)
                    modified = True
                    for k, v in counts.items():
                        total[k] = total.get(k, 0) + v
            choice["message"] = message
            new_choices.append(choice)
        new_response["choices"] = new_choices
        if not modified:
            return None
        return GuardrailResult.modified(response=new_response, reason=f"redacted PII: {total}", counts=total)

    # -- stream rewriting -------------------------------------------------------

    def _stream_keep_from(self, text: str) -> int:
        """Boundary between emit-now and hold-back for one stream step.

        Everything past the reserve (``len(text) - stream_reserve_chars``)
        is held back, because a PII match may still grow as more text
        arrives — an email split across provider chunks must not leak its
        first half. A match that already extends past the reserve boundary
        pulls the boundary further back to its own start, so a match is
        never half-emitted.
        """
        safe = max(0, len(text) - self.stream_reserve_chars)
        keep_from = safe
        for _label, pattern in self._patterns():
            for m in pattern.finditer(text):
                if m.end() > safe:
                    keep_from = min(keep_from, m.start())
        return keep_from

    async def rewrite_stream_text(self, text: str, *, final: bool = False) -> tuple[str, str]:
        """Mask PII in streamed output as it is produced.

        ``text`` is everything not yet emitted (this guardrail's pending
        buffer + the new delta). Non-final steps emit only the fully
        redacted part outside the reserve; the final flush redacts all.
        """
        if final:
            redacted, _counts = self._redact_text(text)
            return redacted, ""
        keep_from = self._stream_keep_from(text)
        emit, _counts = self._redact_text(text[:keep_from])
        return emit, text[keep_from:]
