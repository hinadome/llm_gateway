"""Tests for wire capture: the full request/response picture on log rows.

Beyond the bodies (already stored), rows now carry the allowlisted
client request headers (which SDK called, client correlation ids — never
credentials), the request as actually sent to the provider (method, path,
provider-native body — after model mapping, prompt injection, guardrail
edits), and the allowlisted provider response headers (request ids,
rate-limit state). See ``gateway/observability/wire.py``.
"""

from __future__ import annotations

from gateway.config import LoggingConfig
from gateway.observability import wire

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


async def _latest_row(client, key_name: str | None = None) -> dict:
    params = {"limit": 1}
    if key_name:
        params["key_name"] = key_name
    logs = await client.get("/admin/logs", params=params, headers=auth(ADMIN_KEY))
    entry = logs.json()["data"][0]
    detail = await client.get(f"/admin/logs/{entry['id']}", headers=auth(ADMIN_KEY))
    return detail.json()


async def test_wire_capture_on_log_row(env):
    """One chat request: the row records both wire sides. The upstream
    request is the post-mapping, provider-native form (the client asked
    for "test-model"; the wire body carries the deployment's native
    "model-a"), and only allowlisted headers are stored — the credential
    the client sent never lands in the row."""
    key = (await create_key(env.client, "wire"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="hello wire"),
        headers={
            **auth(key),
            "user-agent": "my-app-sdk/1.2.3",
            "x-stainless-lang": "python",
            "x-custom-header": "not-allowlisted",
        },
    )
    assert resp.status_code == 200
    row = await _latest_row(env.client)

    # client side: allowlisted inbound headers, never the credential
    assert row["client_headers"]["user-agent"] == "my-app-sdk/1.2.3"
    assert row["client_headers"]["x-stainless-lang"] == "python"
    assert "x-custom-header" not in row["client_headers"]
    assert "authorization" not in row["client_headers"]

    # upstream side: what actually went on the wire
    assert row["upstream_request"]["method"] == "POST"
    assert row["upstream_request"]["path"] == "/v1/chat/completions"
    assert row["upstream_request"]["body"]["model"] == "model-a"  # native, not test-model
    assert row["upstream_request"]["body"]["messages"]
    assert row["upstream_headers"]["x-request-id"] == "req_mock_123"
    assert row["upstream_headers"]["x-ratelimit-remaining-requests"] == "41"
    assert "content-type" not in row["upstream_headers"]  # not allowlisted


async def test_wire_capture_streaming(env):
    """Streams go through the same hooks: the row still carries the wire
    request and the provider's response headers."""
    key = (await create_key(env.client, "wire-stream"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="streamed wire", stream=True),
        headers=auth(key),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    row = await _latest_row(env.client)
    assert row["stream"] is True
    assert row["upstream_request"]["path"] == "/v1/chat/completions"
    assert row["upstream_request"]["body"]["model"] == "model-a"
    assert row["upstream_headers"]["x-request-id"] == "req_mock_123"


async def test_wire_capture_cache_hit_records_no_upstream(env):
    """A cache hit never touches the provider: the row's upstream fields
    stay None — not the previous request's wire data (the middleware
    resets the slots per request)."""
    key = (await create_key(env.client, "wire-cache"))["key"]
    body = chat_body(content="identical for cache")
    first = await env.client.post(
        "/v1/chat/completions", json=body, headers=auth(key)
    )
    assert first.status_code == 200
    cached = await env.client.post(
        "/v1/chat/completions", json=body, headers=auth(key)
    )
    assert cached.status_code == 200
    row = await _latest_row(env.client)
    assert row["cache_hit"] is True
    assert row["upstream_request"] is None
    assert row["upstream_headers"] is None


async def test_wire_capture_store_bodies_false(tmp_path):
    """Compliance mode: the upstream body is traffic content and follows
    store_bodies off; method/path (transport metadata) and the header
    snapshots (metadata, like tokens/latency) still land."""
    config = make_config(logging=LoggingConfig(store_bodies=False))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "compliance-wire"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="secret sauce"),
            headers={**auth(key), "user-agent": "compliance-sdk/2.0"},
        )
        assert resp.status_code == 200
        row = await _latest_row(env.client)
        assert row["request_body"] is None
        assert row["response_body"] is None
        assert row["upstream_request"]["method"] == "POST"
        assert row["upstream_request"]["path"] == "/v1/chat/completions"
        assert "body" not in row["upstream_request"]
        assert row["client_headers"]["user-agent"] == "compliance-sdk/2.0"
        assert row["upstream_headers"]["x-request-id"] == "req_mock_123"
        break


async def test_wire_capture_allowlist_config(tmp_path):
    """The allowlists are config: a custom client list replaces the
    default (allow, never block — unknown headers stay out)."""
    config = make_config(
        logging=LoggingConfig(client_header_allowlist=["x-tenant-trace"])
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "custom-allowlist"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="allowlisted"),
            headers={**auth(key), "x-tenant-trace": "abc123", "user-agent": "nope/1.0"},
        )
        assert resp.status_code == 200
        row = await _latest_row(env.client)
        assert row["client_headers"] == {"x-tenant-trace": "abc123"}
        break


async def test_wire_capture_multipart_records_note(env):
    """Multipart uploads (audio transcriptions, image edits) go to the
    provider as httpx *streaming* bodies — the request hook cannot read
    them (and must not consume the stream), so the row records the
    transport shape instead of the body."""
    key = (await create_key(env.client, "wire-multipart"))["key"]
    resp = await env.client.post(
        "/v1/audio/transcriptions",
        data={"model": "test-model"},
        files={"file": ("audio.webm", b"fake-audio-bytes", "audio/webm")},
        headers=auth(key),
    )
    assert resp.status_code == 200, resp.text
    row = await _latest_row(env.client)
    assert row["upstream_request"]["method"] == "POST"
    assert row["upstream_request"]["path"] == "/v1/audio/transcriptions"
    assert "multipart/form-data" in row["upstream_request"]["body"]["content_type"]
    assert row["upstream_request"]["body"]["_note"]
    assert row["upstream_headers"]["x-request-id"] == "req_mock_123"


def test_sanitize_headers_unit():
    """Wildcard prefixes, case-insensitive lookups, value truncation."""
    headers = {
        "X-Ratelimit-Remaining-Requests": "41",
        "x-ratelimit-remaining-tokens": "9987",
        "x-request-id": "req_1",
        "authorization": "Bearer sk-secret",
        "content-type": "application/json",
    }
    out = wire.sanitize_headers(headers, wire.DEFAULT_UPSTREAM_HEADER_ALLOWLIST)
    assert out == {
        "x-request-id": "req_1",
        "x-ratelimit-remaining-requests": "41",
        "x-ratelimit-remaining-tokens": "9987",
    }
    long = {"user-agent": "x" * 10_000}
    out = wire.sanitize_headers(long, ["user-agent"])
    assert len(out["user-agent"]) == 512
