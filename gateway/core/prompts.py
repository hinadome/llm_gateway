"""Gateway-managed prompt policy: system-prompt injection + templating.

Two transformations, applied between model resolution and the guardrail
phases so that everything downstream — guardrails, cache identity,
shadow mirrors, provider calls — sees the final prompt, while
``ctx.request_body`` keeps what the client actually sent:

* **System prompt** — a global default (``prompts.system_prompt``) or a
  per-key one (``VirtualKey.system_prompt``, which wins) is prepended as
  a system message *only when the request carries none*. A client that
  sends its own system message (any inbound format — OpenAI ``system``,
  Anthropic ``system``, Gemini ``systemInstruction``, Responses
  ``instructions`` — all normalize to a system/developer message) is
  never clobbered: the gateway manages the prompt for callers that
  don't provide one.
* **Templating** — ``{{variable}}`` placeholders in message text (and in
  the managed system prompt itself) resolve against a variables map
  (``prompts.variables`` merged with the key's ``prompt_variables``,
  per-key winning). Only *known* names are replaced; unknown placeholders
  pass through untouched, so prompts containing code with braces are
  safe. With no variables configured, the whole step is a no-op.

Both transformations are part of cache identity (like guardrail
modifications): two requests that differ only in unresolved placeholders
are different cache entries; one sent through two keys with different
variables is two entries.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..schemas import ChatCompletionRequest, Message

# {{name}} placeholders: identifier-ish names, optional inner whitespace.
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# Roles that count as "the request carries a system prompt" (OpenAI's
# newer SDKs send "developer"; the native inbound formats normalize
# their system fields to "system").
_SYSTEM_ROLES = frozenset({"system", "developer"})


def substitute(text: str, variables: dict[str, str] | None) -> str:
    """Replace ``{{name}}`` placeholders that are defined in ``variables``.

    Unknown placeholders are returned untouched — deliberately, so that
    content containing literal braces (code snippets, JSON examples)
    survives when no matching variable exists.
    """
    if not variables or "{{" not in text:
        return text
    return _VARIABLE.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)


def _render_message(message: "Message", variables: dict[str, str]) -> tuple["Message", bool]:
    """Resolve placeholders in one message's text content.

    Handles plain string content and content-part lists (only ``text``
    parts are templated — images and other parts pass through). Returns
    the same object when nothing changed.
    """
    content = message.content
    if isinstance(content, str):
        rendered = substitute(content, variables)
        if rendered != content:
            return message.model_copy(update={"content": rendered}), True
    elif isinstance(content, list):
        new_parts = []
        changed = False
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                rendered = substitute(part["text"], variables)
                if rendered != part["text"]:
                    changed = True
                    part = {**part, "text": rendered}
            new_parts.append(part)
        if changed:
            return message.model_copy(update={"content": new_parts}), True
    return message, False


def apply_prompt_policy(
    request: "ChatCompletionRequest",
    system_prompt: str | None,
    variables: dict[str, str] | None,
) -> tuple["ChatCompletionRequest", bool]:
    """Apply the gateway's prompt policy to a chat request.

    Returns ``(request, changed)`` — the same object when the policy did
    nothing (the common case: no prompt configured, or the client sent
    its own), so callers can skip re-copying and log the original.
    """
    changed = False

    # 1. Inject the managed system prompt when the request carries none.
    if system_prompt and not any(
        m.role in _SYSTEM_ROLES for m in request.messages
    ):
        from ..schemas import Message

        rendered_prompt = substitute(system_prompt, variables)
        request = request.model_copy(
            update={
                "messages": [
                    Message(role="system", content=rendered_prompt),
                    *request.messages,
                ]
            }
        )
        changed = True

    # 2. Resolve variables in message text (client messages and any
    #    injected prompt alike).
    if variables:
        rendered = []
        touched = False
        for message in request.messages:
            new_message, message_changed = _render_message(message, variables)
            rendered.append(new_message)
            touched = touched or message_changed
        if touched:
            request = request.model_copy(update={"messages": rendered})
            changed = True

    return request, changed
