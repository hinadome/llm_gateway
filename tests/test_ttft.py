"""TTFT (time-to-first-token) and decode rate on streamed requests.

Streaming latency was a single ``latency_ms`` number; rows now also
record ``ttft_ms`` — milliseconds from request start to the first
content-bearing chunk off the wire — and expose the derived decode
rate (completion tokens per second AFTER the first token, i.e. the
generation phase). Non-streaming rows and cache-hit replays carry
``None`` (nothing went on the wire — the same rule as wire capture).
"""

from __future__ import annotations

import time

from gateway.core.context import RequestContext
from gateway.models import RequestLog
from gateway.api.admin import _decode_rate

from .conftest import ADMIN_KEY, auth, chat_body, create_key, make_config


async def _latest_row(client) -> dict:
    logs = await client.get(
        "/admin/logs", params={"limit": 1}, headers=auth(ADMIN_KEY)
    )
    entry = logs.json()["data"][0]
    detail = await client.get(f"/admin/logs/{entry['id']}", headers=auth(ADMIN_KEY))
    return detail.json()


async def test_streaming_records_ttft(env):
    """A streamed request records time-to-first-token: non-negative,
    within the request's total latency, with a positive decode rate
    whenever the generation phase lasted long enough to measure."""
    key = (await create_key(env.client, "ttft-stream"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="hello streamed", stream=True),
        headers=auth(key),
    )
    assert resp.status_code == 200
    row = await _latest_row(env.client)
    assert row["stream"] is True
    assert row["ttft_ms"] is not None
    assert 0 <= row["ttft_ms"] <= row["latency_ms"]
    # 2 completion tokens in the mock stream: either a measurable
    # (positive) decode rate, or an instant stream where the decode
    # window is too small to divide by.
    assert row["decode_tokens_per_sec"] is None or row["decode_tokens_per_sec"] > 0


async def test_non_streaming_records_no_ttft(env):
    """The whole response arrives at once — there is no "first token"
    to wait for, so the row stays NULL (not the latency again)."""
    key = (await create_key(env.client, "ttft-plain"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="hello plain"),
        headers=auth(key),
    )
    assert resp.status_code == 200
    row = await _latest_row(env.client)
    assert row["stream"] is False
    assert row["ttft_ms"] is None
    assert row["decode_tokens_per_sec"] is None


async def test_cache_hit_stream_records_no_ttft(env):
    """A cached stream replays stored chunks — nothing went on the
    wire, so no first-token time is recorded (the same rule as wire
    capture)."""
    key = (await create_key(env.client, "ttft-cache"))["key"]
    body = chat_body(content="identical for ttft cache", stream=True)
    first = await env.client.post("/v1/chat/completions", json=body, headers=auth(key))
    assert first.status_code == 200
    cached = await env.client.post("/v1/chat/completions", json=body, headers=auth(key))
    assert cached.status_code == 200
    row = await _latest_row(env.client)
    assert row["cache_hit"] is True
    assert row["ttft_ms"] is None


async def test_ttft_in_console_record_and_stats(env, caplog):
    """The structured per-request record carries ttft_ms for streamed
    requests (dropped for non-streaming — formatters skip None), and
    /admin/stats reports TTFT percentiles over streamed rows."""
    import logging

    key = (await create_key(env.client, "ttft-observability"))["key"]
    with caplog.at_level(logging.INFO, logger="gateway.pipeline"):
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello metrics", stream=True),
            headers=auth(key),
        )
        assert resp.status_code == 200
    completed = [r for r in caplog.records if r.getMessage() == "chat request completed"]
    assert completed and getattr(completed[-1], "ttft_ms", None) is not None

    stats = await env.client.get("/admin/stats", headers=auth(ADMIN_KEY))
    ttft = stats.json()["ttft_ms"]
    assert ttft["p50"] is not None and ttft["p95"] is not None


# ----------------------------------------------------------------------
# The computation itself
# ----------------------------------------------------------------------


def test_decode_tokens_per_sec_unit():
    """50 tokens in the 400ms after the first token → 125 tok/s; and
    the degenerate cases (no tokens, no ttft, zero-length decode
    window) yield None instead of a division by zero."""
    ctx = RequestContext(request_id="r")
    ctx.started_monotonic = time.monotonic() - 0.5  # latency = 500ms
    ctx.ttft_ms = 100
    ctx.completion_tokens = 50
    assert ctx.decode_tokens_per_sec() == 125.0

    ctx.completion_tokens = 0
    assert ctx.decode_tokens_per_sec() is None
    ctx.completion_tokens = 50
    ctx.ttft_ms = None
    assert ctx.decode_tokens_per_sec() is None
    ctx.ttft_ms = 500  # decode window = 0ms
    assert ctx.decode_tokens_per_sec() is None


def test_decode_rate_on_log_row():
    """The serializer computes the same rate from the stored row."""
    row = RequestLog(
        request_id="r", ttft_ms=100, latency_ms=500, completion_tokens=50
    )
    assert _decode_rate(row) == 125.0
    assert _decode_rate(RequestLog(request_id="r2", latency_ms=500)) is None
    assert (
        _decode_rate(
            RequestLog(request_id="r3", ttft_ms=500, latency_ms=500, completion_tokens=5)
        )
        is None
    )
