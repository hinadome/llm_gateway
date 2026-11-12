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
                elif part.get("type") == "input_audio":
                    parts.append("[audio]")
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


def audio_mime_for_format(audio_format: str) -> str:
    """OpenAI ``input_audio`` format ('wav' | 'mp3' | ...) -> MIME type."""
    if audio_format in ("mp3", "mpeg"):
        return "audio/mpeg"  # the standard MIME for MP3
    return f"audio/{audio_format}"


def audio_format_for_mime(mime_type: str | None) -> str:
    """MIME type -> OpenAI ``input_audio`` format (subtype, mp3-normalized).

    Inverse of :func:`audio_mime_for_format`: ``audio/mpeg`` and
    ``audio/mp3`` both become ``mp3``; parameters ride along
    (``audio/L16;rate=24000`` -> ``L16;rate=24000``).
    """
    subtype = (mime_type or "wav").split("audio/")[-1]
    if subtype in ("mpeg", "mp3"):
        return "mp3"
    return subtype


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
