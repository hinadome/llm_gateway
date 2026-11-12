"""Tests for the prompt library (prompt ops): versioned named prompts,
per-key references, and A/B mirroring to a candidate version.

A key's ``system_prompt_ref`` names a library prompt; its **active**
version serves (fail-closed 400 when the name has no active version —
same rule as a broken model mapping). Editing the prompt is a version
bump + activate: no key change, no restart, instant rollback. A second
**candidate** version can A/B against the active one: a fraction of the
successful requests served with the active version is mirrored to the
candidate in the background (the candidate never serves clients), with
comparison rows in the request log flagged ``shadow`` + ``prompt_variant``
— so they are excluded from /admin/stats and never add to key spend,
exactly like shadow-deployment mirrors.
"""

from __future__ import annotations

import asyncio

from gateway.core.prompt_library import PromptLibrary

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _wait_for
from .test_pipeline import read_sse


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _make_prompt(client, name: str, body: str, notes: str | None = None) -> dict:
    resp = await client.post(
        "/admin/prompts",
        json={"name": name, "body": body, "notes": notes},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _patch_prompt(client, prompt_id: int, **fields) -> dict:
    resp = await client.patch(
        f"/admin/prompts/{prompt_id}", json=fields, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _versions(client, name: str) -> list[dict]:
    resp = await client.get(f"/admin/prompts/{name}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _log_rows(client, **params) -> list[dict]:
    resp = await client.get("/admin/logs", params=params, headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _upstream_system(env, index: int) -> str:
    """The system message content of the nth upstream request."""
    messages = env.mock.requests[index]["body"]["messages"]
    (system,) = [m for m in messages if m["role"] in ("system", "developer")]
    return system["content"]


# ----------------------------------------------------------------------
# Library CRUD (admin API)
# ----------------------------------------------------------------------


async def test_first_version_auto_activates(env):
    created = await _make_prompt(env.client, "persona", "You are v1.")
    assert created["version"] == 1
    assert created["is_active"] is True
    assert created["is_candidate"] is False

    (only,) = await _versions(env.client, "persona")
    assert only["is_active"] is True


async def test_new_versions_inactive_until_activated(env):
    v1 = await _make_prompt(env.client, "persona", "You are v1.")
    v2 = await _make_prompt(env.client, "persona", "You are v2.")
    assert v2["version"] == 2
    assert v2["is_active"] is False  # v1 still serves

    patched = await _patch_prompt(env.client, v2["id"], activate=True)
    assert patched["is_active"] is True

    versions = {v["version"]: v for v in await _versions(env.client, "persona")}
    assert versions[2]["is_active"] is True
    assert versions[1]["is_active"] is False  # exactly one active per name


async def test_candidate_unique_per_name(env):
    v1 = await _make_prompt(env.client, "persona", "You are v1.")
    v2 = await _make_prompt(env.client, "persona", "You are v2.")
    v3 = await _make_prompt(env.client, "persona", "You are v3.")

    await _patch_prompt(env.client, v2["id"], candidate=True, ab_fraction=0.5)
    assert (await _versions(env.client, "persona"))[0]["is_candidate"] is False  # newest

    # Candidating v3 uncandidates v2 — one challenger per name.
    await _patch_prompt(env.client, v3["id"], candidate=True, ab_fraction=1.0)
    versions = {v["version"]: v for v in await _versions(env.client, "persona")}
    assert versions[3]["is_candidate"] is True
    assert versions[2]["is_candidate"] is False

    # Activating a candidate promotes it out of the challenger slot too.
    await _patch_prompt(env.client, v3["id"], activate=True)
    versions = {v["version"]: v for v in await _versions(env.client, "persona")}
    assert versions[3]["is_active"] is True
    assert versions[3]["is_candidate"] is False
    assert versions[1]["is_active"] is False  # the original active stepped down


async def test_ab_fraction_clamped(env):
    v1 = await _make_prompt(env.client, "persona", "You are v1.")
    high = await _patch_prompt(env.client, v1["id"], ab_fraction=5.0)
    assert high["ab_fraction"] == 1.0
    low = await _patch_prompt(env.client, v1["id"], ab_fraction=-1.0)
    assert low["ab_fraction"] == 0.0


async def test_delete_refuses_active_until_repointed(env):
    v1 = await _make_prompt(env.client, "persona", "You are v1.")
    v2 = await _make_prompt(env.client, "persona", "You are v2.")

    # The active version is refused — keys reference the name.
    resp = await env.client.delete(
        f"/admin/prompts/{v1['id']}", headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 409
    assert "active version" in resp.json()["error"]["message"]
    assert len(await _versions(env.client, "persona")) == 2

    # Repoint serving first, then the old version can go.
    await _patch_prompt(env.client, v2["id"], activate=True)
    resp = await env.client.delete(
        f"/admin/prompts/{v1['id']}", headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1
    remaining = await _versions(env.client, "persona")
    assert [v["version"] for v in remaining] == [2]


async def test_prompt_routes_404s(env):
    resp = await env.client.get("/admin/prompts/nope", headers=auth(ADMIN_KEY))
    assert resp.status_code == 404

    resp = await env.client.patch(
        "/admin/prompts/999999", json={"activate": True}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 404

    resp = await env.client.delete("/admin/prompts/999999", headers=auth(ADMIN_KEY))
    assert resp.status_code == 404


async def test_list_prompt_summaries(env):
    await _make_prompt(env.client, "persona", "You are v1.")
    v2 = await _make_prompt(env.client, "persona", "You are v2.")
    await _make_prompt(env.client, "other", "Other prompt.")
    await _patch_prompt(env.client, v2["id"], candidate=True, ab_fraction=0.25)

    resp = await env.client.get("/admin/prompts", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()["data"]}

    assert by_name["persona"]["versions"] == 2
    assert by_name["persona"]["active"]["version"] == 1
    assert by_name["persona"]["candidate"]["version"] == 2
    assert by_name["persona"]["candidate"]["ab_fraction"] == 0.25

    assert by_name["other"]["versions"] == 1
    assert by_name["other"]["active"]["version"] == 1
    assert by_name["other"]["candidate"] is None


# ----------------------------------------------------------------------
# Serving: named references on keys
# ----------------------------------------------------------------------


async def test_ref_beats_inline_beats_global(tmp_path):
    config = make_config(prompts=make_config().prompts)
    config.prompts.system_prompt = "You are the global prompt."
    async for env in _make_env(tmp_path, config=config):
        await _make_prompt(env.client, "persona", "You are the library prompt.")
        key = (
            await create_key(
                env.client,
                "ref-user",
                system_prompt="You are the inline prompt.",
                system_prompt_ref="persona",
            )
        )["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        assert _upstream_system(env, 0) == "You are the library prompt."
        break


async def test_inline_still_beats_global_without_ref(tmp_path):
    config = make_config()
    config.prompts.system_prompt = "You are the global prompt."
    async for env in _make_env(tmp_path, config=config):
        key = (
            await create_key(env.client, "inline-user", system_prompt="Inline wins.")
        )["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        assert _upstream_system(env, 0) == "Inline wins."
        break


async def test_missing_ref_fails_closed(env):
    key = (await create_key(env.client, "broken-ref", system_prompt_ref="ghost"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(), headers=auth(key)
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "prompt_not_found"
    assert error["param"] == "system_prompt_ref"
    assert "ghost" in error["message"]


async def test_activate_changes_serving_without_restart(tmp_path):
    async for env in _make_env(tmp_path):
        await _make_prompt(env.client, "persona", "You are v1.")
        v2 = await _make_prompt(env.client, "persona", "You are v2.")
        key = (await create_key(env.client, "switcher", system_prompt_ref="persona"))[
            "key"
        ]

        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="first"),
            headers=auth(key),
        )
        assert first.status_code == 200
        assert _upstream_system(env, 0) == "You are v1."

        await _patch_prompt(env.client, v2["id"], activate=True)

        # Distinct content: cache identity includes the injected prompt,
        # but a fresh ask keeps the test independent of cache behaviour.
        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="second"),
            headers=auth(key),
        )
        assert second.status_code == 200
        assert _upstream_system(env, 1) == "You are v2."
        break


async def test_deleted_version_keeps_key_serving(tmp_path):
    async for env in _make_env(tmp_path):
        v1 = await _make_prompt(env.client, "persona", "You are v1.")
        v2 = await _make_prompt(env.client, "persona", "You are v2.")
        await _patch_prompt(env.client, v2["id"], activate=True)
        key = (await create_key(env.client, "survivor", system_prompt_ref="persona"))[
            "key"
        ]
        resp = await env.client.delete(
            f"/admin/prompts/{v1['id']}", headers=auth(ADMIN_KEY)
        )
        assert resp.status_code == 200

        served = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert served.status_code == 200
        assert _upstream_system(env, 0) == "You are v2."
        break


async def test_library_cache_ttl_covers_cross_instance_writes(tmp_path):
    """Admin writes invalidate this process's cache; the TTL is the
    backstop for replicas that did not see the write."""
    async for env in _make_env(tmp_path):
        await _make_prompt(env.client, "persona", "You are v1.")
        v2 = await _make_prompt(env.client, "persona", "You are v2.")

        # A second library instance = "another replica": it does not see
        # the admin API's invalidation, only its own TTL.
        replica = PromptLibrary(cache_seconds=0.05)
        first = await replica.resolve("persona")
        assert first is not None and first.version == 1

        await _patch_prompt(env.client, v2["id"], activate=True)  # app-side write

        still_cached = await replica.resolve("persona")
        assert still_cached is not None and still_cached.version == 1

        await asyncio.sleep(0.06)
        expired = await replica.resolve("persona")
        assert expired is not None and expired.version == 2
        break


# ----------------------------------------------------------------------
# A/B mirroring to the candidate version
# ----------------------------------------------------------------------


async def _ab_env(tmp_path, *, fraction: float = 1.0, active_body="You are v1.",
                  candidate_body="You are v2."):
    """Env with prompt 'persona' (v1 active, v2 candidate) + a keyed client."""
    async for env in _make_env(tmp_path):
        await _make_prompt(env.client, "persona", active_body)
        v2 = await _make_prompt(env.client, "persona", candidate_body)
        await _patch_prompt(env.client, v2["id"], candidate=True, ab_fraction=fraction)
        key = (await create_key(env.client, "ab-user", system_prompt_ref="persona"))[
            "key"
        ]
        yield env, key


async def test_ab_mirror_row_written(tmp_path):
    async for env, key in _ab_env(tmp_path):
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="compare me"), headers=auth(key)
        )
        assert resp.status_code == 200
        request_id = resp.headers["x-request-id"]

        # The mirror lands in the background: primary + mirrored copy.
        await _wait_for(lambda: len(env.mock.requests) >= 2, what="mirror sent")

        # The client was served by the ACTIVE version...
        assert _upstream_system(env, 0) == "You are v1."
        # ...and the mirror carried the CANDIDATE.
        assert _upstream_system(env, 1) == "You are v2."
        assert env.mock.requests[1]["body"]["stream"] is False

        async def _mirror_row() -> dict | None:
            rows = await _log_rows(env.client, request_id=request_id)
            return next((r for r in rows if r["shadow"]), None)

        await _wait_for(_mirror_row, what="mirror log row written")
        rows = await _log_rows(env.client, request_id=request_id)
        primary = next(r for r in rows if not r["shadow"])
        mirror = next(r for r in rows if r["shadow"])
        assert primary["prompt_variant"] is None
        assert mirror["prompt_variant"] == "persona v2"
        assert mirror["status"] == "success"
        assert mirror["stream"] is False  # mirrors are always comparable completes

        # The mirror row's stored body shows the candidate prompt.
        detail = await env.client.get(
            f"/admin/logs/{mirror['id']}", headers=auth(ADMIN_KEY)
        )
        body = detail.json()["request_body"]
        assert body["messages"][0] == {"role": "system", "content": "You are v2."}

        # Excluded from /admin/stats (client traffic only) ...
        stats = (
            await env.client.get("/admin/stats", headers=auth(ADMIN_KEY))
        ).json()
        assert stats["total_requests"] == 1
        (key_stats,) = [k for k in stats["by_key"] if k["key_name"] == "ab-user"]
        assert key_stats["requests"] == 1

        # ... and from key spend (mirror rows have no key_id).
        keys = (
            await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))
        ).json()["data"]
        ab_key = next(k for k in keys if k["name"] == "ab-user")
        expected = round(10 * 2.0 / 1_000_000 + 7 * 4.0 / 1_000_000, 6)
        assert ab_key["spend_usd"] == expected  # one request, not two
        break


async def test_ab_fraction_zero_mirrors_nothing(tmp_path):
    async for env, key in _ab_env(tmp_path, fraction=0.0):
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="just once"), headers=auth(key)
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.1)  # give any (wrong) mirror a chance to fire
        assert len(env.mock.requests) == 1
        rows = await _log_rows(env.client, key_name="ab-user")
        assert len(rows) == 1 and not rows[0]["shadow"]
        break


async def test_candidate_never_serves_clients(tmp_path):
    """The mock replies with a PII-shaped answer when it sees the marker —
    a client seeing it would mean the candidate had served real traffic."""
    async for env, key in _ab_env(
        tmp_path,
        active_body="You are v1.",
        candidate_body="You are v2. PIIPLEASE",
    ):
        for n in range(3):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content=f"ask {n}"),
                headers=auth(key),
            )
            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"] == "Hello from the mock!"
        await _wait_for(lambda: len(env.mock.requests) >= 6, what="mirrors sent")

        # ... while every mirror got the marker-shaped reply. Mirrors are
        # fire-and-forget tasks, so they interleave with the primaries —
        # identify them by what they carried, not by position.
        mirror_texts = [
            r["body"]["messages"][0]["content"]
            for r in env.mock.requests
            if r["body"]["messages"][0].get("role") == "system"
            and "v2" in str(r["body"]["messages"][0].get("content"))
        ]
        assert len(mirror_texts) == 3
        assert all("PIIPLEASE" in t for t in mirror_texts)
        break


async def test_client_system_message_skips_mirror(tmp_path):
    """Client-supplied system messages always win — the library prompt
    never served, so there is nothing to A/B."""
    async for env, key in _ab_env(tmp_path):
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "Client persona."},
                    {"role": "user", "content": "hello"},
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.1)
        assert len(env.mock.requests) == 1
        assert _upstream_system(env, 0) == "Client persona."
        rows = await _log_rows(env.client, key_name="ab-user")
        assert len(rows) == 1 and not rows[0]["shadow"]
        break


async def test_guardrail_rewritten_system_skips_mirror(tmp_path):
    """PII redaction rewrote the injected system message after the prompt
    policy ran — the mirror's swap target no longer matches, so the
    comparison is skipped rather than run against a polluted prompt."""
    async for env, key in _ab_env(
        tmp_path, active_body="You are v1. Email alice@example.com."
    ):
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hello"), headers=auth(key)
        )
        assert resp.status_code == 200
        # The primary was served with the redacted system message...
        assert "alice@example.com" not in _upstream_system(env, 0)
        await asyncio.sleep(0.1)
        # ... and no mirror fired (the active body no longer matches).
        assert len(env.mock.requests) == 1
        rows = await _log_rows(env.client, key_name="ab-user")
        assert len(rows) == 1 and not rows[0]["shadow"]
        break


async def test_ab_metrics_exposed(tmp_path):
    async for env, key in _ab_env(tmp_path):
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="meter me"), headers=auth(key)
        )
        assert resp.status_code == 200
        await _wait_for(lambda: len(env.mock.requests) >= 2, what="mirror sent")
        metrics_body = (await env.client.get("/metrics")).text
        assert 'gateway_prompt_mirror_requests_total' in metrics_body
        assert 'prompt="persona"' in metrics_body
        assert 'gateway_prompt_mirror_latency_seconds' in metrics_body
        break


async def test_streamed_request_mirrored_nonstream(tmp_path):
    async for env, key in _ab_env(tmp_path):
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="stream me", stream=True),
            headers=auth(key),
        )
        assert resp.status_code == 200
        chunks = await read_sse(resp)
        assert chunks  # client stream unaffected

        await _wait_for(lambda: len(env.mock.requests) >= 2, what="mirror sent")
        assert _upstream_system(env, 1) == "You are v2."

        async def _mirror_row() -> dict | None:
            rows = await _log_rows(env.client, key_name="ab-user")
            return next((r for r in rows if r["shadow"]), None)

        await _wait_for(_mirror_row, what="mirror log row written")
        rows = await _log_rows(env.client, key_name="ab-user")
        mirror = next(r for r in rows if r["shadow"])
        assert mirror["prompt_variant"] == "persona v2"
        assert mirror["stream"] is False  # mirrors stay comparable completes
        break
