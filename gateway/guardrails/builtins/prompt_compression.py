"""Prompt-compression guardrail.

The remediation counterpart of ``context_window``: where that guardrail
*blocks* (or redirects) a request whose prompt exceeds the model's
window, this one **compresses** it to fit — deterministically, in the
request phase, with no LLM call (the whole point is that the fix costs
nothing and adds no latency).

The algorithm is middle-out folding, the shape agent frameworks
converged on for long conversations:

1. **Fold the middle.** Leading *system* messages are always kept
   (policy, small, load-bearing). The first ``keep_first`` non-system
   messages are kept (the task establishment), the last ``keep_last``
   non-system messages are kept (the live exchange), and everything
   between is dropped. A short note is prepended to the first kept
   message's text — content only, no structural changes: roles and
   content shapes are preserved, so every provider adapter (and every
   native outbound format) handles the folded request exactly like the
   original. The note names how many messages and roughly how many
   tokens went missing, so the model knows the history has a gap.
2. **Truncate oldest-first.** If the estimate is still over the
   target, kept non-system messages are trimmed toward
   ``min_message_chars`` one at a time, oldest first. System messages
   and the *last* message are never truncated — the standing policy
   and the newest turn are sacred; a response to a mangled task or a
   mangled latest turn is garbage either way.
3. **Step aside.** If the target still cannot be met (typically one
   gigantic final message), the guardrail does nothing: the request
   flows to ``context_window`` (ordered after it in the config, the
   intended pairing) or to the provider for the honest error.
   Compression is a saver, not a blocker — it never blocks.

The target: ``max_input_tokens`` when set, else the model's declared
context window minus ``reserve_output_tokens`` (the same vocabulary and
default ``context_window`` uses, so compress-then-check composes:
a request folded to ``window - reserve`` passes the context_window
guardrail that follows it). Models with neither are skipped.

Like every guardrail it composes with the framework for free: observe
mode logs the would-be savings without touching the request, scoping
limits it to keys/models/routes, ``disabled_guardrails`` turns it off
per key at runtime, and the verdict lands in ``guardrail_actions`` with
before/after token counts. Modifications run before the cache lookup,
so compressed and uncompressed forms of one logical prompt hold
different cache entries — different wires, different entries.
"""

from __future__ import annotations

from typing import Any

from ...schemas import ChatCompletionRequest, Message
from ...utils import content_to_text, estimate_tokens
from ..base import Guardrail, GuardrailResult

# Prepended to the first kept message when the middle is folded (the
# model deserves to know its history has a gap) and appended to each
# truncated message.
_FOLD_NOTE = (
    "[gateway: {dropped} earlier message{plural} (~{tokens} tokens) "
    "were removed from this conversation to fit the context window.]\n"
)
_TRUNC_NOTE = "\n[...gateway: this message was truncated to fit the context window.]"


class PromptCompressionGuardrail(Guardrail):
    name = "prompt_compression"
    description = (
        "Compresses over-budget prompts to fit, deterministically "
        "(no LLM call): middle-out folding — keep system messages, the "
        "first keep_first and the last keep_last messages, drop the "
        "middle with a marker note — then oldest-first truncation "
        "toward min_message_chars. Never blocks: when even compression "
        "cannot reach the target (a gigantic last message), it steps "
        "aside and lets context_window or the provider answer."
    )

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        # Explicit input-token budget. None = target the model's
        # declared context window instead (minus the response reserve).
        self.max_input_tokens: int | None = self.params.get("max_input_tokens")
        # Response budget subtracted from the model's window when
        # targeting the window — same name/default as context_window,
        # so a request compressed to the target passes a context_window
        # guardrail ordered after this one.
        self.reserve_output_tokens: int = self.params.get(
            "reserve_output_tokens", 1024
        )
        # Middle-out fold: non-system messages kept from the top and
        # bottom. The bottom is the live exchange; the top establishes
        # the task. keep_last must keep at least the active turn.
        self.keep_first: int = self.params.get("keep_first", 2)
        self.keep_last: int = self.params.get("keep_last", 4)
        # Floor for oldest-first per-message truncation (chars).
        self.min_message_chars: int = self.params.get("min_message_chars", 512)
        self._validate()

    def _validate(self) -> None:
        if self.max_input_tokens is not None and self.max_input_tokens < 1:
            raise ValueError("prompt_compression: max_input_tokens must be >= 1")
        if self.reserve_output_tokens < 0:
            raise ValueError("prompt_compression: reserve_output_tokens must be >= 0")
        if self.keep_first < 0:
            raise ValueError("prompt_compression: keep_first must be >= 0")
        if self.keep_last < 1:
            raise ValueError(
                "prompt_compression: keep_last must be >= 1 (the active turn "
                "is never dropped)"
            )
        if self.min_message_chars < 1:
            raise ValueError("prompt_compression: min_message_chars must be >= 1")

    # -- helpers --------------------------------------------------------------

    def _target_for(self, ctx) -> int | None:
        """The input-token budget to compress to, or None = unmanaged."""
        if self.max_input_tokens is not None:
            return self.max_input_tokens
        if ctx.context_window is not None:
            return max(1, ctx.context_window - self.reserve_output_tokens)
        return None

    @staticmethod
    def _tokens(messages: list[Message]) -> int:
        # The same ~4-chars/token heuristic context_window uses; image
        # and audio parts count as their placeholders (documented
        # under-count, the trade-off every pre-flight check here makes).
        return sum(estimate_tokens(content_to_text(m.content)) for m in messages)

    @staticmethod
    def _split_leading_system(messages: list[Message]) -> tuple[list[Message], list[Message]]:
        """Leading system messages (always kept) and the rest."""
        i = 0
        while i < len(messages) and messages[i].role == "system":
            i += 1
        return messages[:i], messages[i:]

    @staticmethod
    def _with_prepended_note(message: Message, note: str) -> Message:
        """Prepend plain text to a message, preserving its content shape."""
        content = message.content
        if content is None:
            return message.model_copy(update={"content": note})
        if isinstance(content, str):
            return message.model_copy(update={"content": note + content})
        if isinstance(content, list):
            # Content-parts list: the note becomes the first text part.
            parts = list(content)
            parts.insert(0, {"type": "text", "text": note})
            return message.model_copy(update={"content": parts})
        return message  # unknown shape: never mangle it

    @staticmethod
    def _truncate_message(message: Message, max_chars: int) -> Message:
        """Shorten a message's text toward max_chars, in place by copy.

        String content is cut; content-parts lists have their text
        parts shortened (non-text parts pass through untouched — an
        image is worth the same tokens however much text surrounds it).
        A message already within the budget is returned unchanged.
        """

        def _cut(text: str) -> str:
            if len(text) <= max_chars:
                return text
            return text[:max_chars] + _TRUNC_NOTE

        content = message.content
        if isinstance(content, str):
            new = _cut(content)
            if new is content:
                return message
            return message.model_copy(update={"content": new})
        if isinstance(content, list):
            new_parts = [
                {**p, "text": _cut(p["text"])}
                if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                else p
                for p in content
            ]
            if new_parts == content:
                return message
            return message.model_copy(update={"content": new_parts})
        return message

    # -- request phase --------------------------------------------------------

    async def check_request(
        self, request: ChatCompletionRequest, ctx
    ) -> GuardrailResult | None:
        target = self._target_for(ctx)
        if target is None:
            return None  # no explicit budget and the model declares no window

        before = self._tokens(request.messages)
        if before <= target:
            return None  # already fits: compression is not a no-op tax

        messages = list(request.messages)
        dropped = 0

        # 1. Middle-out fold: keep leading system + first/keep_last,
        #    drop the middle, mark the fold on the first kept message.
        system_block, rest = self._split_leading_system(messages)
        if len(rest) > self.keep_first + self.keep_last:
            head, middle, tail = (
                rest[: self.keep_first],
                rest[self.keep_first : len(rest) - self.keep_last],
                rest[len(rest) - self.keep_last :],
            )
            dropped = len(middle)
            dropped_tokens = self._tokens(middle)
            note = _FOLD_NOTE.format(
                dropped=dropped,
                plural="" if dropped == 1 else "s",
                tokens=dropped_tokens,
            )
            kept = head + tail
            if kept:
                kept[0] = self._with_prepended_note(kept[0], note)
            messages = system_block + kept

        truncated = 0

        # 2. Oldest-first truncation toward min_message_chars, until
        #    the estimate fits. System messages and the last message
        #    are never trimmed.
        if self._tokens(messages) > target:
            # Candidates: every kept non-system message except the
            # final one, oldest first.
            last_index = len(messages) - 1
            candidates = [
                i
                for i, m in enumerate(messages)
                if i != last_index and m.role != "system"
            ]
            for i in candidates:
                if self._tokens(messages) <= target:
                    break
                shortened = self._truncate_message(messages[i], self.min_message_chars)
                if shortened is not messages[i]:
                    messages[i] = shortened
                    truncated += 1

        after = self._tokens(messages)
        if after > target:
            # Could not reach the target (a gigantic last message or a
            # prompt with nothing foldable/trimmable). Step aside: the
            # request flows on untouched and context_window (or the
            # provider, or nothing) gives the honest error. Compression
            # is a saver, not a blocker.
            return None

        # The saving is real only when it was enforced (observe mode
        # and replay-harness runs never touch counters — the same
        # discipline as GUARDRAIL_BLOCKS).
        saved = before - after
        if saved > 0 and self.mode != "observe" and not ctx.dry_run:
            from ...observability import metrics

            metrics.PROMPT_COMPRESSION_SAVED.labels(model=ctx.model or "").inc(saved)

        return GuardrailResult.modified(
            messages=messages,
            reason=(
                f"compressed prompt: {before} -> {after} tokens "
                f"(target {target}; dropped {dropped} middle message"
                f"{'s' if dropped != 1 else ''}, truncated {truncated})"
            ),
            tokens_before=before,
            tokens_after=after,
            tokens_saved=saved,
            messages_dropped=dropped,
            messages_truncated=truncated,
            target=target,
        )
