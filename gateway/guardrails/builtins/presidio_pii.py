"""Presidio-backed PII guardrail (production-grade detector).

Uses Microsoft `presidio-analyzer` to detect PII in prompts (and optionally
responses), then either redacts it (replace spans with ``<ENTITY_TYPE>``
placeholders) or blocks the request for entities you name in
``block_entities``.

Requires the optional ``guardrails`` extra::

    uv sync --extra guardrails        # or: uv add presidio-analyzer

By default the full ``AnalyzerEngine`` runs, which loads the spacy model
presidio depends on (en_core_web_lg, ~425 MB RAM per process) and adds
context-aware scoring and NER entities (``PERSON``, ``LOCATION``). Set
``use_nlp: false`` for a pattern-only engine: no model load, lower accuracy,
same entity names for the pattern-detectable subset.

Analysis runs in a worker thread (``asyncio.to_thread``) so the event loop
is never blocked; the engine itself is constructed once and shared
(presidio's recommended deployment shape — one engine, many analyses).

Params:

- ``entities``           — entity types to detect (default: curated set below).
- ``block_entities``     — subset of ``entities`` that block instead of redact.
- ``score_threshold``    — minimum presidio score (default 0.3; presidio
                           pattern matches score 0.4-1.0, weak ones 0.05).
- ``mask_in_response``   — also redact the model's output (default false).
- ``placeholder``        — replacement template, ``{ENTITY}`` slot
                           (default ``"<{ENTITY}>"``, presidio's convention).
- ``use_nlp``            — full NLP engine (default true) vs pattern-only.
- ``language``           — presidio language (default "en").

Like the regex builtin, this guardrail checks the request and (optionally)
response phases; streamed output is not re-analyzed per chunk (see §8.2 of
ARCHITECTURE.md — stream guardrails are block-only and per-chunk full-text
analysis is O(n²)).
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...schemas import ChatCompletionRequest, Message
from ..base import Guardrail, GuardrailResult

# Curated default: high-value PII that is reliable to detect. Deliberately
# NOT "everything presidio supports" — unrestricted runs surface noisy
# neighbours (e.g. UK_NHS on US phone numbers, URL fragments inside emails).
DEFAULT_ENTITIES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "IP_ADDRESS",
    "US_SSN",
    "US_ITIN",
    "US_BANK_NUMBER",
    "PERSON",
    "LOCATION",
]

# Pattern-only mode (use_nlp: false): recognizers that work without the
# spacy model. NER entities (PERSON, LOCATION) are unavailable there.
_PATTERN_RECOGNIZER_NAMES = (
    "EmailRecognizer",
    "PhoneRecognizer",
    "CreditCardRecognizer",
    "IbanRecognizer",
    "IpRecognizer",
    "UsSsnRecognizer",
    "UsItinRecognizer",
    "UsBankRecognizer",
    "UrlRecognizer",
    "UsLicenseRecognizer",
    "UsPassportRecognizer",
    "CryptoRecognizer",
)

# Shared engine instances ( AnalyzerEngine is expensive: model load).
# Keyed by language; pattern-only guardrails share nothing.
_ENGINE_CACHE: dict[str, Any] = {}


def _import_error() -> ImportError:
    return ImportError(
        "The presidio_pii guardrail requires the presidio-analyzer package. "
        "Install the optional 'guardrails' extra: uv sync --extra guardrails "
        "(or: uv add presidio-analyzer)."
    )


class PresidioPIIGuardrail(Guardrail):
    name = "presidio_pii"
    description = (
        "Detect and redact PII with Microsoft Presidio (emails, phones, cards, "
        "SSNs, IBANs, IPs, names/places with NLP); optionally block per entity."
    )

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer import predefined_recognizers as pr
        except ImportError as exc:  # pragma: no cover - depends on env
            raise _import_error() from exc

        self.entities: list[str] = list(self.params.get("entities", DEFAULT_ENTITIES))
        self.block_entities: set[str] = set(self.params.get("block_entities", []))
        self.score_threshold: float = float(self.params.get("score_threshold", 0.3))
        self.mask_in_response: bool = bool(self.params.get("mask_in_response", False))
        self.placeholder: str = self.params.get("placeholder", "<{ENTITY}>")
        self.language: str = self.params.get("language", "en")
        self.use_nlp: bool = bool(self.params.get("use_nlp", True))

        if unknown := (self.block_entities - set(self.entities)):
            raise ValueError(
                f"presidio_pii: block_entities must be a subset of entities "
                f"(unknown: {sorted(unknown)})"
            )

        if self.use_nlp:
            engine = _ENGINE_CACHE.get(self.language)
            if engine is None:
                # Loads the spacy model (slow, once per process).
                engine = AnalyzerEngine()
                _ENGINE_CACHE[self.language] = engine
            self._engine = engine
            self._recognizers: dict[str, Any] = {}
            supported = set(engine.get_supported_entities())
        else:
            self._engine = None
            self._recognizers = {}
            for cls_name in _PATTERN_RECOGNIZER_NAMES:
                recognizer = getattr(pr, cls_name)()
                for entity in recognizer.supported_entities:
                    self._recognizers[entity] = recognizer
            supported = set(self._recognizers)
            # The default entity set includes NER types (PERSON, LOCATION)
            # that pattern-only mode cannot detect. Drop them silently when
            # the user did not ask for them explicitly; an explicit list
            # that names unsupported entities still fails loudly below.
            if "entities" not in self.params:
                self.entities = [e for e in self.entities if e in supported]
        if unknown := (set(self.entities) - supported):
            raise ValueError(
                f"presidio_pii: unknown entities for this engine: {sorted(unknown)} "
                f"(available include: {sorted(supported)[:12]}...)"
            )

    # -- analysis ----------------------------------------------------------

    def _analyze(self, text: str) -> list[Any]:
        """Detect PII spans (scores already above the threshold)."""
        if self._engine is not None:
            return list(
                self._engine.analyze(
                    text=text,
                    language=self.language,
                    entities=self.entities,
                    score_threshold=self.score_threshold,
                )
            )
        results = []
        for entity, recognizer in self._recognizers.items():
            if entity not in self.entities:
                continue
            for result in recognizer.analyze(
                text=text, entities=[entity], nlp_artifacts=None
            ):
                if result.score >= self.score_threshold:
                    results.append(result)
        return results

    def _redact(self, text: str) -> tuple[str, dict[str, int], set[str]]:
        """Replace detected spans with placeholders.

        Overlapping spans (possible when several entity types are enabled)
        are resolved greedily: highest score wins, ties by longer span.
        """
        results = self._analyze(text)
        counts: dict[str, int] = {}
        if not results:
            return text, counts, set()
        taken: list[Any] = []
        for result in sorted(
            results, key=lambda r: (-r.score, -(r.end - r.start), r.start)
        ):
            if any(result.start < t.end and t.start < result.end for t in taken):
                continue
            taken.append(result)
        taken.sort(key=lambda r: r.start)

        out: list[str] = []
        last = 0
        for result in taken:
            out.append(text[last : result.start])
            out.append(self.placeholder.format(ENTITY=result.entity_type))
            counts[result.entity_type] = counts.get(result.entity_type, 0) + 1
            last = result.end
        out.append(text[last:])
        return "".join(out), counts, {r.entity_type for r in taken}

    # -- content traversal (mirrors pii_redaction) --------------------------

    def _redact_content(self, content: Any) -> tuple[Any, dict[str, int], set[str]]:
        total: dict[str, int] = {}
        hit: set[str] = set()
        if isinstance(content, str):
            new_text, counts, hit = self._redact(content)
            return new_text, counts, hit
        if isinstance(content, list):
            new_parts: list[Any] = []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ):
                    new_text, counts, hit = self._redact(part["text"])
                    for k, v in counts.items():
                        total[k] = total.get(k, 0) + v
                    new_parts.append({**part, "text": new_text})
                else:
                    new_parts.append(part)
            return new_parts, total, hit
        return content, total, hit

    def _redact_messages(
        self, messages: list[Message]
    ) -> tuple[list[Message], dict[str, int], set[str]]:
        total: dict[str, int] = {}
        entities: set[str] = set()
        new_messages: list[Message] = []
        for message in messages:
            new_content, counts, hit = self._redact_content(message.content)
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v
            entities |= hit
            if new_content is message.content:
                new_messages.append(message)
            else:
                new_messages.append(message.model_copy(update={"content": new_content}))
        return new_messages, total, entities

    # -- phases ----------------------------------------------------------------

    async def check_request(
        self, request: ChatCompletionRequest, ctx
    ) -> GuardrailResult | None:
        # Presidio analysis is CPU-bound (spacy NER); run it off the loop.
        new_messages, counts, entities = await asyncio.to_thread(
            self._redact_messages, request.messages
        )
        blocked = entities & self.block_entities
        if blocked:
            return GuardrailResult.block(
                f"prompt contains blocked PII: {sorted(blocked)}",
                entities=sorted(blocked),
                counts=counts,
            )
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
                new_text, counts, _ = await asyncio.to_thread(self._redact, content)
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
                new_transcript, counts, _ = await asyncio.to_thread(
                    self._redact, str(audio["transcript"])
                )
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
        return GuardrailResult.modified(
            response=new_response, reason=f"redacted PII: {total}", counts=total
        )
