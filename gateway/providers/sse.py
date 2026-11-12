"""Server-Sent-Events line parsing for provider streams."""

from __future__ import annotations

from typing import AsyncIterator


async def sse_data_lines(byte_chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Yield the payload of every ``data:`` line from an SSE byte stream.

    Non-data lines (``event:``, comments, etc.) are ignored — the three
    providers we support carry everything needed in the data payload.
    """
    buffer = ""
    async for raw in byte_chunks:
        buffer += raw.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("data:"):
                yield line[5:].strip()
