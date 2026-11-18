"""Feedback capture: client-reported quality for logged requests.

Covers the contract: ratings ride the chat scope; ownership follows
the files/batches rule (foreign/unknown ids 404, the platform admin
may rate anything); admin list + stats with the forced tenant scoping
tenant admins get everywhere else; the metric and the
``feedback.recorded`` lifecycle event fire; validation is strict
(1..5, comment bounded); streams and failed requests are rateable
too — any row in the request log is.
"""

from __future__ import annotations

from gateway.config import EventHandlerConfig, EventsConfig
from gateway.observability.metrics import FEEDBACK

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_tenants import _create_tenant


async def _make_chat(env, key: str, model: str = "test-model") -> str:
    """One successful chat request → its X-Request-Id."""
    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(model=model), headers=auth(key)
    )
    assert resp.status_code == 200, resp.text
    return resp.headers["X-Request-Id"]


async def _rate(env, key: str, request_id: str, rating: int = 5, comment: str | None = None):
    body = {"request_id": request_id, "rating": rating}
    if comment is not None:
        body["comment"] = comment
    return await env.client.post("/v1/feedback", json=body, headers=auth(key))


async def test_feedback_roundtrip_and_listing(env):
    key = (await create_key(env.client, "rater"))["key"]
    request_id = await _make_chat(env, key)
    resp = await _rate(env, key, request_id, rating=5, comment="great")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == request_id
    assert body["rating"] == 5

    listed = await env.client.get("/admin/feedback", headers=auth(ADMIN_KEY))
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == request_id
    assert row["key_name"] == "rater"
    assert row["model"] == "test-model"
    assert row["rating"] == 5
    assert row["comment"] == "great"
    # Multiple signals per request are allowed (a client may correct).
    again = await _rate(env, key, request_id, rating=2)
    assert again.status_code == 200
    total = (
        await env.client.get(
            "/admin/feedback", params={"request_id": request_id}, headers=auth(ADMIN_KEY)
        )
    ).json()["total"]
    assert total == 2


async def test_feedback_validation(env):
    key = (await create_key(env.client, "valid"))["key"]
    request_id = await _make_chat(env, key)
    # Validation surfaces as OpenAI-style 400s (the app's validation
    # handler converts FastAPI's 422s).
    for bad in (0, 6, "three"):
        resp = await _rate(env, key, request_id, rating=bad)
        assert resp.status_code == 400, bad
    resp = await _rate(env, key, request_id, rating=3, comment="x" * 2001)
    assert resp.status_code == 400
    assert (
        await env.client.post(
            "/v1/feedback", json={"rating": 5}, headers=auth(key)
        )
    ).status_code == 400  # missing request_id


async def test_feedback_unknown_request_404(env):
    key = (await create_key(env.client, "ghost"))["key"]
    resp = await _rate(env, key, "req_does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "feedback_target_not_found"


async def test_feedback_foreign_request_404(env):
    key_a = (await create_key(env.client, "owner-a"))["key"]
    key_b = (await create_key(env.client, "sneaky-b"))["key"]
    request_id = await _make_chat(env, key_a)
    # A foreign id gets the same 404 as an unknown one — never confirm
    # another key's request exists.
    resp = await _rate(env, key_b, request_id)
    assert resp.status_code == 404
    # The owner still can.
    assert (await _rate(env, key_a, request_id)).status_code == 200


async def test_platform_admin_rates_any_request(env):
    key = (await create_key(env.client, "plain"))["key"]
    request_id = await _make_chat(env, key)
    resp = await _rate(env, ADMIN_KEY, request_id, rating=1, comment="bad")
    assert resp.status_code == 200
    # The admin's rating attributes to the *request's* key (stats group
    # by who was served, not who pressed the button).
    listed = await env.client.get("/admin/feedback", headers=auth(ADMIN_KEY))
    assert listed.json()["data"][0]["key_name"] == "plain"


async def test_feedback_stats(env):
    key = (await create_key(env.client, "stats"))["key"]
    for model, rating in (
        ("test-model", 5),
        ("test-model", 4),
        ("test-model", 1),
        ("other-model", 2),
    ):
        request_id = await _make_chat(env, key, model=model)
        resp = await _rate(env, key, request_id, rating=rating)
        assert resp.status_code == 200, resp.text

    stats = await env.client.get("/admin/feedback/stats", headers=auth(ADMIN_KEY))
    assert stats.status_code == 200
    body = stats.json()
    assert body["overall"]["count"] == 4
    assert body["overall"]["avg_rating"] == 3.0
    assert body["overall"]["pct_negative"] == 50.0
    assert body["overall"]["pct_positive"] == 50.0
    test_model = body["by_model"]["test-model"]
    assert test_model["count"] == 3
    assert test_model["avg_rating"] == 3.333  # round(10/3, 3)
    assert test_model["pct_positive"] == 66.7
    assert body["by_model"]["other-model"]["count"] == 1
    assert body["by_key"]["stats"]["count"] == 4

    # Filters compose with the aggregates.
    filtered = (
        await env.client.get(
            "/admin/feedback/stats",
            params={"model": "other-model"},
            headers=auth(ADMIN_KEY),
        )
    ).json()
    assert filtered["overall"]["count"] == 1
    assert "test-model" not in filtered["by_model"]


async def test_feedback_tenant_scoping(tmp_path):
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        acme_key = (
            await create_key(env.client, "acme-member", tenant_id=acme["id"])
        )["key"]
        beta_key = (
            await create_key(env.client, "beta-member", tenant_id=beta["id"])
        )["key"]
        acme_admin = (
            await create_key(env.client, "acme-admin", is_admin=True, tenant_id=acme["id"])
        )["key"]

        request_id = await _make_chat(env, acme_key)
        assert (await _rate(env, acme_key, request_id)).status_code == 200
        beta_request = await _make_chat(env, beta_key)
        assert (await _rate(env, beta_key, beta_request, rating=2)).status_code == 200

        # Tenant admin: forced tenant filter in the list...
        listed = await env.client.get(
            "/admin/feedback", headers=auth(acme_admin)
        )
        rows = listed.json()["data"]
        assert [r["key_name"] for r in rows] == ["acme-member"]
        # ...and in the stats, even when asking for another tenant.
        stats = (
            await env.client.get(
                "/admin/feedback/stats",
                params={"tenant_id": beta["id"]},
                headers=auth(acme_admin),
            )
        ).json()
        assert stats["overall"]["count"] == 1
        assert "beta-member" not in stats["by_key"]

        # Platform admin sees everything.
        all_stats = (
            await env.client.get("/admin/feedback/stats", headers=auth(ADMIN_KEY))
        ).json()
        assert all_stats["overall"]["count"] == 2


async def test_feedback_requires_chat_scope(env):
    # Feedback rides the chat scope: a key scoped to embeddings only
    # cannot rate (it cannot chat either).
    scoped = (
        await create_key(env.client, "emb-only", allowed_endpoints=["embeddings"])
    )["key"]
    key = (await create_key(env.client, "chatter"))["key"]
    request_id = await _make_chat(env, key)
    resp = await _rate(env, scoped, request_id)
    assert resp.status_code == 403


async def test_feedback_metric_and_event(tmp_path):
    from .test_events import Recorder

    config = make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        recorder = env.app.state.events.handlers[0]
        key = (await create_key(env.client, "metered"))["key"]
        request_id = await _make_chat(env, key)

        before = FEEDBACK.labels(rating="4")._value.get()
        resp = await _rate(env, key, request_id, rating=4)
        assert resp.status_code == 200
        assert FEEDBACK.labels(rating="4")._value.get() == before + 1

        await env.app.state.events.drain()
        recorded = [e for e in recorder.events if e.name == "feedback.recorded"]
        assert recorded and recorded[0].payload["request_id"] == request_id
        assert recorded[0].payload["model"] == "test-model"
        assert recorded[0].payload["rating"] == 4


async def test_streaming_request_feedback(env):
    key = (await create_key(env.client, "streamer"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(stream=True),
        headers=auth(key),
    )
    assert resp.status_code == 200
    request_id = resp.headers["X-Request-Id"]
    assert (await _rate(env, key, request_id, rating=5)).status_code == 200
    row = (
        await env.client.get(
            "/admin/feedback", params={"request_id": request_id}, headers=auth(ADMIN_KEY)
        )
    ).json()["data"]
    assert row and row[0]["rating"] == 5


async def test_failed_request_is_rateable(env):
    # Any logged request can be rated — including a failed one (the
    # client may want to rate the failure itself). A blocked request's
    # id never reached the response headers, so it comes from the log.
    key = (await create_key(env.client, "unlucky"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(model="no-such-model"),
        headers=auth(key),
    )
    assert resp.status_code == 404
    logs = await env.client.get(
        "/admin/logs", params={"limit": 1, "status": "error"}, headers=auth(ADMIN_KEY)
    )
    request_id = logs.json()["data"][0]["request_id"]
    rated = await _rate(env, key, request_id, rating=1, comment="404s now")
    assert rated.status_code == 200
    row = (
        await env.client.get(
            "/admin/feedback", params={"request_id": request_id}, headers=auth(ADMIN_KEY)
        )
    ).json()["data"]
    assert row and row[0]["comment"] == "404s now"
