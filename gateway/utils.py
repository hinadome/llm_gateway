"""Small shared helpers used across the gateway."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator


def content_to_text(content: Any) -> str:
    """Flatten OpenAI message content (string or content-parts list) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    parts.append(str(part["text"]))
                elif part.get("type") == "image_url":
                    parts.append("[image]")
            elif isinstance(part, str):
                parts.append(part)
    return "".join(parts)


def iter_message_texts(messages: list[Any]) -> Iterator[str]:
    """Yield the text of every message (system/user/assistant/tool)."""
    for message in messages:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        text = content_to_text(content)
        if text:
            yield text
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                if fn is not None:
                    yield f"{getattr(fn, 'name', '')} {getattr(fn, 'arguments', '')}"


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for pre-flight checks."""
    return max(1, len(text) // 4)


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15] + "...<truncated>"


def daily_bucket(now: datetime) -> str:
    """Bucket id for the daily spend window — a UTC calendar day.

    Lexicographically ordered (``2026-11-11``), so string comparison can
    stand in for date comparison. Windows roll at 00:00 UTC.
    """
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def monthly_bucket(now: datetime) -> str:
    """Bucket id for the monthly spend window — a UTC calendar month."""
    return now.astimezone(timezone.utc).strftime("%Y-%m")
