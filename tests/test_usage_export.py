"""Tests for the usage export endpoint (GET /admin/usage/export).

Billing / chargeback extract: request rows in a time window (ISO-8601
``since``/``until``, defaulting to the last 30 days), filterable by key,
model and status. JSON adds aggregates (totals + per-key / per-model /
per-status breakdowns); CSV is one row per request with a stable column
order. Mirrored (shadow) calls are excluded by default — they never cost
the client — and can be included with ``include_shadow=true``.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest

from gateway.db import get_sessionmaker
from gateway.models import RequestLog

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _wait_for
from .test_shadow_deployments import _shadow_config

CSV_COLUMNS = [
    "request_id",
    "created_at",
    "key_name",
    "tenant_id",
    "model",
    "provider",
    "deployment",
    "status",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd",
    "latency_ms",
    "cache_hit",
    "stream",
    "shadow",
]


def _pricing_both() -> dict:
    return {
        "model-a": {"input": 2.0, "output": 4.0},
        "model-c": {"input": 2.0, "output": 4.0},
    }


async def _seed_traffic(env, key_a: str, key_b: str) -> list[float]:
    """Two keys, four client requests (one is a cache replay).

    Returns the per-request costs of the three *billed* requests (the
    cache replay costs nothing: its row carries no cost).
    """
    miss_a = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="billable one"),
        headers=auth(key_a),
    )
    cached_a = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="billable one"),  # exact replay
        headers=auth(key_a),
    )
    miss_b = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="billable two"),
        headers=auth(key_b),
    )
    miss_b_other = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(model="other-model", content="other model"),
        headers=auth(key_b),
    )
    assert all(
        r.status_code == 200 for r in (miss_a, cached_a, miss_b, miss_b_other)
    )
    assert cached_a.headers["x-gateway-cache"] == "HIT"
    return [
        float(miss_a.headers["x-gateway-cost-usd"]),
        float(miss_b.headers["x-gateway-cost-usd"]),
        float(miss_b_other.headers["x-gateway-cost-usd"]),
    ]


async def _export_csv(env, **params) -> tuple[list[list[str]], list[str]]:
    resp = await env.client.get(
        "/admin/usage/export",
        params={"format": "csv", **params},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith("attachment; filename=")
    rows = list(csv.reader(io.StringIO(resp.text)))
    return rows[1:], rows[0]  # data rows, header


async def _export_json(env, **params) -> dict:
    resp = await env.client.get(
        "/admin/usage/export", params=params, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ----------------------------------------------------------------------


async def test_csv_export_rows_and_header(tmp_path):
    config = make_config(pricing=_pricing_both())
    async for env in _make_env(tmp_path, config=config):
        key_a = (await create_key(env.client, "team-a"))["key"]
        key_b = (await create_key(env.client, "team-b"))["key"]
        costs = await _seed_traffic(env, key_a, key_b)

        data, header = await _export_csv(env)
        assert header == CSV_COLUMNS
        assert len(data) == 4  # the cache replay is a row too (unbilled)

        # billed row: tokens, cost, no flags (tenant_id is column 3 —
        # these platform keys carry none)
        billed = [r for r in data if r[2] == "team-b" and r[4] == "test-model"]
        assert len(billed) == 1
        row = billed[0]
        assert row[3] == ""
        assert row[7] == "success"
        assert row[8] == "10" and row[9] == "7" and row[10] == "17"
        assert float(row[11]) == costs[1]
        assert row[13] == "false" and row[15] == "false"

        # the cache replay: flagged, provider=cache, free
        replay = [r for r in data if r[2] == "team-a"][1]
        assert replay[5] == "cache"
        assert float(replay[11]) == 0.0
        assert replay[13] == "true"

        # rows are ordered oldest-first (stable for billing ingestion)
        created = [r[1] for r in data]
        assert created == sorted(created)
        break


async def test_json_export_summary_and_groups(tmp_path):
    config = make_config(pricing=_pricing_both())
    async for env in _make_env(tmp_path, config=config):
        key_a = (await create_key(env.client, "team-a"))["key"]
        key_b = (await create_key(env.client, "team-b"))["key"]
        costs = await _seed_traffic(env, key_a, key_b)

        body = await _export_json(env)
        assert body["object"] == "usage_export"
        summary = body["summary"]
        assert summary["requests"] == 4
        assert summary["row_count"] == 4
        assert summary["truncated"] is False
        # tokens include the (free) cache replay's usage; cost does not
        assert summary["prompt_tokens"] == 40  # 4 rows x 10
        assert summary["completion_tokens"] == 28  # 4 rows x 7
        assert summary["total_tokens"] == 68
        assert summary["cost_usd"] == pytest.approx(sum(costs), abs=1e-9)
        assert summary["by_status"] == {
            "success": {"requests": 4, "cost_usd": pytest.approx(sum(costs), abs=1e-9)}
        }

        by_key = {entry["key_name"]: entry for entry in summary["by_key"]}
        assert by_key["team-a"]["requests"] == 2
        assert by_key["team-a"]["cost_usd"] == pytest.approx(costs[0], abs=1e-9)
        assert by_key["team-b"]["requests"] == 2
        assert by_key["team-b"]["cost_usd"] == pytest.approx(
            costs[1] + costs[2], abs=1e-9
        )

        by_model = {entry["model"]: entry for entry in summary["by_model"]}
        assert by_model["test-model"]["requests"] == 3
        assert by_model["other-model"]["requests"] == 1
        # grouped by cost, most expensive first (billing convenience)
        costs_grouped = [entry["cost_usd"] for entry in summary["by_key"]]
        assert costs_grouped == sorted(costs_grouped, reverse=True)
        break


async def test_filters_key_model_status(tmp_path):
    config = make_config(pricing=_pricing_both())
    async for env in _make_env(tmp_path, config=config):
        key_a = (await create_key(env.client, "team-a"))["key"]
        key_b = (await create_key(env.client, "team-b"))["key"]
        await _seed_traffic(env, key_a, key_b)
        # a blocked request (output moderation) for team-b
        blocked = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="BADOUTPUT"),
            headers=auth(key_b),
        )
        assert blocked.status_code == 200  # content_filter body
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="plain three"),
            headers=auth(key_a),
        )

        # key filter
        body = await _export_json(env, key_name="team-a")
        assert body["summary"]["requests"] == 3
        assert {r["key_name"] for r in body["requests"]} == {"team-a"}

        # model filter
        body = await _export_json(env, model="other-model")
        assert body["summary"]["requests"] == 1
        assert body["requests"][0]["model"] == "other-model"

        # status filter: the blocked request is its own category
        body = await _export_json(env, status="blocked")
        assert body["summary"]["requests"] == 1
        assert body["requests"][0]["status"] == "blocked"
        assert body["summary"]["by_status"] == {
            "blocked": {"requests": 1, "cost_usd": 0.0}
        }
        break


async def test_time_window_filters_rows(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "modern-user"))["key"]
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="recent traffic"),
            headers=auth(key),
        )
        # a row from 2020, outside the default 30-day window
        async with get_sessionmaker()() as session:
            session.add(
                RequestLog(
                    request_id="ancient-1",
                    key_name="ancient-key",
                    model="test-model",
                    provider="openai",
                    deployment="openai/primary",
                    status="success",
                    created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                    prompt_tokens=1,
                    completion_tokens=1,
                    total_tokens=2,
                    cost_usd=9.99,
                )
            )
            await session.commit()

        # default window (last 30 days): only the recent request
        body = await _export_json(env)
        assert body["summary"]["requests"] == 1
        assert body["requests"][0]["key_name"] == "modern-user"

        # explicit window reaching back: both rows
        body = await _export_json(env, since="2019-01-01T00:00:00Z")
        assert body["summary"]["requests"] == 2
        assert body["summary"]["cost_usd"] == pytest.approx(
            sum(r["cost_usd"] for r in body["requests"]), abs=1e-9
        )

        # until bound: only the ancient row (the modern request is far later)
        body = await _export_json(
            env, since="2019-01-01T00:00:00Z", until="2020-06-01T00:00:00Z"
        )
        assert body["summary"]["requests"] == 1
        assert body["requests"][0]["request_id"] == "ancient-1"
        break


async def test_shadow_rows_excluded_unless_asked(tmp_path):
    async for env in _make_env(tmp_path, config=_shadow_config()):
        key = (await create_key(env.client, "mirrored-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="mirror me for billing"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        request_id = resp.headers["x-request-id"]
        await _wait_for(
            lambda: len(env.mock.requests) >= 2, what="shadow mirror sent"
        )

        async def _shadow_row_present() -> bool:
            body = await _export_json(env, include_shadow=True)
            return any(r["shadow"] for r in body["requests"])

        await _wait_for(_shadow_row_present, what="shadow row written")

        # default: shadow rows excluded (they never cost the client)
        body = await _export_json(env)
        assert body["summary"]["requests"] == 1
        assert body["requests"][0]["shadow"] is False

        # include_shadow: two rows for the same request_id
        body = await _export_json(env, include_shadow=True)
        assert body["summary"]["requests"] == 2
        assert {r["shadow"] for r in body["requests"]} == {True, False}
        assert all(r["request_id"] == request_id for r in body["requests"])
        break


async def test_parameter_validation(tmp_path):
    async for env in _make_env(tmp_path):
        resp = await env.client.get(
            "/admin/usage/export",
            params={"since": "2026-02-01", "until": "2026-01-01"},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 400

        resp = await env.client.get(
            "/admin/usage/export",
            params={"since": "not-a-timestamp"},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 400

        # invalid format is rejected (the app maps validation errors to
        # its OpenAI-style 400 envelope)
        resp = await env.client.get(
            "/admin/usage/export",
            params={"format": "xml"},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 400
        break


async def test_admin_auth_required(tmp_path):
    async for env in _make_env(tmp_path):
        assert (
            await env.client.get("/admin/usage/export")
        ).status_code == 401
        break
