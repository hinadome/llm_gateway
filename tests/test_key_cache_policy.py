"""Per-key cache policy (tier 54) — the §8.2 item-6 gap closes.

Until tier 54 the response caches were global: one ``cache.enabled``
and one ``cache.ttl_seconds`` for every caller, so a team wanting no
caching (a support bot that must answer fresh) or a longer window (a
slow-moving catalog) had no key-level say. The contract now, snapshotted
at auth like the other key ceilings:

* ``cache_enabled`` — NULL = inherit the global switch; ``false`` =
  this key bypasses BOTH response caches (exact + semantic, chat and
  embeddings alike) for lookup AND store: never served from a cache,
  never writing entries, never counting a miss, answering
  ``X-Gateway-Cache: BYPASS``. Narrowing only — a globally-off cache
  stays off for every key (global-off keeps MISS for compat; BYPASS is
  the per-key opt-out's vocabulary).
* ``cache_ttl_seconds`` — NULL = the global window; otherwise the
  key's own, honored on BOTH sides: a hit serves only entries within
  the caller's window (read-side freshness — an entry too stale for
  this caller is a miss WITHOUT being deleted; longer-window callers
  keep serving from it), and an entry the key writes lives the
  writer's window (write-side lifetime — a window longer than the
  global one actually materializes).

Idempotency is deliberately untouched (retry safety ≠ caching): it
wraps the same backends with default arguments, keeps its exact
one-entry-one-key namespace, and replays for opted-out callers
exactly as for everyone else. No per-key size bound — the LRU store
is shared; opt-out and window are the useful knobs.
"""

from __future__ import annotations

from gateway.config import CacheConfig, SemanticCacheConfig
from gateway.observability.metrics import CACHE

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_embeddings import EMB_MODEL, EMB_PRICING, emb_body
from .test_semantic_cache import _topic_embedder


def _misses() -> float:
    return CACHE.labels("miss")._value.get()


async def _chat(env, key: str, content: str = "Hello", **extra):
    return await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=content, **extra),
        headers=auth(key),
    )


def _age_entries(env, seconds: float) -> None:
    """Time-travel every in-memory cache entry back by ``seconds`` —
    no sleeps; the store keeps (stored_at, entry_ttl, response)
    tuples, so aging is a tuple rewrite."""
    cache = env.app.state.pipeline.cache
    for entry_key, (stored_at, ttl, response) in list(cache._store.items()):
        cache._store[entry_key] = (stored_at - seconds, ttl, response)


# ----------------------------------------------------------------------
# Bypass: opted out of both caches, lookup and store
# ----------------------------------------------------------------------


async def test_default_key_control_hit(tmp_path):
    # The control: with no per-key policy nothing changes — the
    # global cache serves the second identical request.
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "plain"))["key"]
        first = await _chat(env, key, "fresh as ever")
        assert first.status_code == 200, first.text
        assert first.headers["X-Gateway-Cache"] == "MISS"
        second = await _chat(env, key, "fresh as ever")
        assert second.headers["X-Gateway-Cache"] == "HIT"


async def test_opt_out_bypasses_lookup_and_store(tmp_path):
    async for env in _make_env(tmp_path):
        opt = (await create_key(env.client, "no-cache", cache_enabled=False))["key"]
        plain = (await create_key(env.client, "plain"))["key"]

        misses = _misses()
        first = await _chat(env, opt, "cache me not")
        second = await _chat(env, opt, "cache me not")
        assert first.headers["X-Gateway-Cache"] == "BYPASS"
        assert second.headers["X-Gateway-Cache"] == "BYPASS"
        # Both requests executed — nothing was served from a cache...
        assert len(env.mock.bodies()) == 2
        # ...nothing was stored (a later plain caller finds no entry)...
        third = await _chat(env, plain, "cache me not")
        assert third.headers["X-Gateway-Cache"] == "MISS"
        # ...and no bypass ever counted a miss — the only one is the
        # plain caller's, which consulted the cache.
        assert _misses() == misses + 1


async def test_opt_out_stream_bypasses(tmp_path):
    # The streaming path carries the same policy: bypassed lookup, and
    # the stream-completion store never lands either.
    async for env in _make_env(tmp_path):
        opt = (await create_key(env.client, "no-cache", cache_enabled=False))["key"]
        plain = (await create_key(env.client, "plain"))["key"]
        first = await _chat(env, opt, "stream me", stream=True)
        second = await _chat(env, opt, "stream me", stream=True)
        assert first.headers["X-Gateway-Cache"] == "BYPASS"
        assert second.headers["X-Gateway-Cache"] == "BYPASS"
        third = await _chat(env, plain, "stream me", stream=True)
        assert third.headers["X-Gateway-Cache"] == "MISS"


async def test_semantic_opt_out(tmp_path):
    # A bypass is BOTH caches: the opted-out key neither hits nor
    # writes the semantic layer, near-duplicates included.
    config = make_config(
        semantic_cache=SemanticCacheConfig(enabled=True, threshold=0.9)
    )
    async for env in _make_env(tmp_path, config=config, semantic_embedder=_topic_embedder):
        plain = (await create_key(env.client, "plain"))["key"]
        opt = (await create_key(env.client, "no-cache", cache_enabled=False))["key"]

        # Control: the plain pair semantic-hits (and stores once).
        p1 = await _chat(env, plain, "topicA first phrasing")
        p2 = await _chat(env, plain, "topicA second phrasing")
        assert p1.headers["X-Gateway-Cache"] == "MISS"
        assert p2.headers["X-Gateway-Cache"] == "SEMANTIC"

        # The opted-out key's near-duplicate of the SAME topic answers
        # BYPASS — executed, not served from the semantic entry...
        o1 = await _chat(env, opt, "topicA third phrasing")
        o2 = await _chat(env, opt, "topicA fourth phrasing")
        assert o1.headers["X-Gateway-Cache"] == "BYPASS"
        assert o2.headers["X-Gateway-Cache"] == "BYPASS"
        # ...and it wrote nothing semantically: still exactly the one
        # entry the plain caller stored.
        assert len(env.app.state.pipeline.semantic._entries) == 1


# ----------------------------------------------------------------------
# The window: read-side freshness, write-side lifetime
# ----------------------------------------------------------------------


async def test_ttl_read_side_freshness(tmp_path):
    async for env in _make_env(tmp_path):
        plain = (await create_key(env.client, "plain"))["key"]
        jumpy = (await create_key(env.client, "jumpy", cache_ttl_seconds=60))["key"]
        body = "the same question"

        # The plain caller stores under the global window (300s).
        assert (await _chat(env, plain, body)).headers["X-Gateway-Cache"] == "MISS"
        _age_entries(env, 100)  # inside the global window

        # The plain caller still serves — nothing changed for it...
        assert (await _chat(env, plain, body)).headers["X-Gateway-Cache"] == "HIT"
        # ...but 100s is past the 60-second caller: the SAME entry is
        # a freshness miss for it (the plain HIT just proved the entry
        # is alive — a freshness miss is not an expiry).
        assert (await _chat(env, jumpy, body)).headers["X-Gateway-Cache"] == "MISS"
        # The freshness miss executed and stored within ITS window:
        # a retry hits.
        assert (await _chat(env, jumpy, body)).headers["X-Gateway-Cache"] == "HIT"


async def test_ttl_write_side_extends_entry_lifetime(tmp_path):
    async for env in _make_env(tmp_path):
        slow = (await create_key(env.client, "slow", cache_ttl_seconds=600))["key"]
        plain = (await create_key(env.client, "plain"))["key"]

        # The long-window caller's write stamps ITS window (600s), not
        # the global 300 — still serving at 400.
        assert (await _chat(env, slow, "slow news")).headers["X-Gateway-Cache"] == "MISS"
        _age_entries(env, 400)
        assert (await _chat(env, slow, "slow news")).headers["X-Gateway-Cache"] == "HIT"

        # The entry lives the WRITER's window for every reader — the
        # window narrows reads, it never shortens another caller's
        # entry — so the plain caller serves from the long write too.
        assert (await _chat(env, plain, "slow news")).headers["X-Gateway-Cache"] == "HIT"

        # Control in the same env: the global window stays the default
        # — a plain caller's own write is gone at 400.
        assert (await _chat(env, plain, "fast news")).headers["X-Gateway-Cache"] == "MISS"
        _age_entries(env, 400)
        assert (await _chat(env, plain, "fast news")).headers["X-Gateway-Cache"] == "MISS"


async def test_write_stamps_the_callers_window(tmp_path):
    # Unit-level through the live pipeline: the store tuple carries
    # the writer's window — 60 for the windowed key, the configured
    # global (300) for the default one.
    async for env in _make_env(tmp_path):
        plain = (await create_key(env.client, "plain"))["key"]
        windowed = (
            await create_key(env.client, "windowed", cache_ttl_seconds=60)
        )["key"]
        await _chat(env, plain, "default stamp")
        await _chat(env, windowed, "windowed stamp")
        ttls = {
            ttl for _, (_, ttl, _) in env.app.state.pipeline.cache._store.items()
        }
        assert ttls == {300.0, 60.0}


# ----------------------------------------------------------------------
# Embeddings: the same policy on the deterministic surface
# ----------------------------------------------------------------------


async def test_embeddings_opt_out_and_window(tmp_path):
    config = make_config(models=[EMB_MODEL], pricing=EMB_PRICING)
    async for env in _make_env(tmp_path, config=config):
        opt = (await create_key(env.client, "no-cache", cache_enabled=False))["key"]
        plain = (await create_key(env.client, "plain"))["key"]
        jumpy = (await create_key(env.client, "jumpy", cache_ttl_seconds=60))["key"]

        first = await env.client.post(
            "/v1/embeddings", json=emb_body("vectorize me"), headers=auth(opt)
        )
        second = await env.client.post(
            "/v1/embeddings", json=emb_body("vectorize me"), headers=auth(opt)
        )
        assert first.status_code == 200, first.text
        assert first.headers["X-Gateway-Cache"] == "BYPASS"
        assert second.headers["X-Gateway-Cache"] == "BYPASS"
        # Nothing stored: the plain caller starts from scratch.
        third = await env.client.post(
            "/v1/embeddings", json=emb_body("vectorize me"), headers=auth(plain)
        )
        assert third.headers["X-Gateway-Cache"] == "MISS"
        fourth = await env.client.post(
            "/v1/embeddings", json=emb_body("vectorize me"), headers=auth(plain)
        )
        assert fourth.headers["X-Gateway-Cache"] == "HIT"

        # The window rides the embeddings read too: at 100 old the
        # entry is past the 60-second caller but inside the global.
        _age_entries(env, 100)
        fifth = await env.client.post(
            "/v1/embeddings", json=emb_body("vectorize me"), headers=auth(jumpy)
        )
        assert fifth.headers["X-Gateway-Cache"] == "MISS"
        sixth = await env.client.post(
            "/v1/embeddings", json=emb_body("vectorize me"), headers=auth(plain)
        )
        assert sixth.headers["X-Gateway-Cache"] == "HIT"


# ----------------------------------------------------------------------
# Admin API: create echo, validation, live PATCH
# ----------------------------------------------------------------------


async def test_create_echo_and_validation(tmp_path):
    async for env in _make_env(tmp_path):
        row = await create_key(
            env.client, "policied", cache_enabled=False, cache_ttl_seconds=120
        )
        assert row["cache_enabled"] is False
        assert row["cache_ttl_seconds"] == 120
        detail = (
            await env.client.get(f"/admin/keys/{row['id']}", headers=auth(ADMIN_KEY))
        ).json()
        assert detail["cache_enabled"] is False
        assert detail["cache_ttl_seconds"] == 120

        # Validation: the window must be >= 1 second — the app's 400
        # envelope answers both create and PATCH.
        bad = await env.client.post(
            "/admin/keys",
            json={"name": "bad", "cache_ttl_seconds": 0},
            headers=auth(ADMIN_KEY),
        )
        assert bad.status_code == 400
        bad_patch = await env.client.patch(
            f"/admin/keys/{row['id']}",
            json={"cache_ttl_seconds": 0},
            headers=auth(ADMIN_KEY),
        )
        assert bad_patch.status_code == 400


async def test_patch_toggles_live(tmp_path):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "policied")
        key, kid = row["key"], row["id"]
        body = "patch me"

        assert (await _chat(env, key, body)).headers["X-Gateway-Cache"] == "MISS"
        assert (await _chat(env, key, body)).headers["X-Gateway-Cache"] == "HIT"

        # Opt out live — the existing entry stops serving this caller.
        patch = await env.client.patch(
            f"/admin/keys/{kid}", json={"cache_enabled": False}, headers=auth(ADMIN_KEY)
        )
        assert patch.status_code == 200
        assert patch.json()["cache_enabled"] is False
        assert (await _chat(env, key, body)).headers["X-Gateway-Cache"] == "BYPASS"

        # Reset to inherit (explicit null — the dialog-is-the-whole-key
        # contract): the entry was never deleted, and it serves again.
        patch = await env.client.patch(
            f"/admin/keys/{kid}", json={"cache_enabled": None}, headers=auth(ADMIN_KEY)
        )
        assert patch.status_code == 200
        assert patch.json()["cache_enabled"] is None
        assert (await _chat(env, key, body)).headers["X-Gateway-Cache"] == "HIT"


async def test_patch_window_live(tmp_path):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "windowed")
        key, kid = row["key"], row["id"]
        body = "window patch"

        assert (await _chat(env, key, body)).headers["X-Gateway-Cache"] == "MISS"

        # Shrink the window live: at 100 old, the (global-window) entry
        # is too stale for a 60-second caller — a freshness miss.
        await env.client.patch(
            f"/admin/keys/{kid}", json={"cache_ttl_seconds": 60}, headers=auth(ADMIN_KEY)
        )
        _age_entries(env, 100)
        assert (await _chat(env, key, body)).headers["X-Gateway-Cache"] == "MISS"

        # Reset to the global window (explicit null): the row echoes
        # null and the caller serves again with no window of its own.
        patch = await env.client.patch(
            f"/admin/keys/{kid}",
            json={"cache_ttl_seconds": None},
            headers=auth(ADMIN_KEY),
        )
        assert patch.status_code == 200
        assert patch.json()["cache_ttl_seconds"] is None
        assert (await _chat(env, key, body)).headers["X-Gateway-Cache"] == "HIT"


# ----------------------------------------------------------------------
# Boundaries: global-off, idempotency, tenants
# ----------------------------------------------------------------------


async def test_global_off_stays_off_for_every_key(tmp_path):
    # Narrowing only: the override can turn caching OFF for one key,
    # never back ON against a global `enabled: false` — and global-off
    # keeps answering MISS for every key (BYPASS is the per-key
    # opt-out's vocabulary, not the global switch's).
    config = make_config(cache=CacheConfig(enabled=False))
    async for env in _make_env(tmp_path, config=config):
        plain = (await create_key(env.client, "plain"))["key"]
        opt = (await create_key(env.client, "no-cache", cache_enabled=False))["key"]
        slow = (await create_key(env.client, "slow", cache_ttl_seconds=600))["key"]

        body = "off then"
        assert (await _chat(env, plain, body)).headers["X-Gateway-Cache"] == "MISS"
        assert (await _chat(env, plain, body)).headers["X-Gateway-Cache"] == "MISS"
        assert (await _chat(env, opt, body)).headers["X-Gateway-Cache"] == "MISS"
        assert (await _chat(env, slow, body)).headers["X-Gateway-Cache"] == "MISS"
        # No per-key policy can widen the global switch: nothing stored.
        assert env.app.state.pipeline.cache._store == {}


async def test_idempotency_unaffected_by_opt_out(tmp_path):
    # Retry safety ≠ caching: Idempotency-Key replay is deliberately
    # untouched by the opt-out — the replay is the whole point of the
    # header, cache policy or not.
    async for env in _make_env(tmp_path):
        opt = (await create_key(env.client, "no-cache", cache_enabled=False))["key"]
        headers = {**auth(opt), "Idempotency-Key": "retry-1"}
        first = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="retry me"), headers=headers
        )
        second = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="retry me"), headers=headers
        )
        assert first.headers["X-Gateway-Cache"] == "BYPASS"
        assert second.headers["X-Gateway-Cache"] == "IDEMPOTENT"
        # One upstream call: the replay served the retry.
        assert len(env.mock.bodies()) == 1
        # Without the header the same caller is back to BYPASS.
        third = await _chat(env, opt, "retry me")
        assert third.headers["X-Gateway-Cache"] == "BYPASS"
        assert len(env.mock.bodies()) == 2


async def test_tenant_keys_carry_their_own_windows(tmp_path):
    # The policy rides tenanted keys (the _auth_context snapshot), and
    # same-tenant keys share entries: each caller's window is its own
    # staleness tolerance over the shared store.
    async for env in _make_env(tmp_path):
        tenant = (
            await env.client.post(
                "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        steady = (await create_key(env.client, "steady", tenant_id=tenant["id"]))["key"]
        jumpy = (
            await create_key(
                env.client, "jumpy", tenant_id=tenant["id"], cache_ttl_seconds=60
            )
        )["key"]

        assert (await _chat(env, steady, "shared question")).headers[
            "X-Gateway-Cache"
        ] == "MISS"
        # Same policy token → shared entry: the sibling hits at age 0.
        assert (await _chat(env, jumpy, "shared question")).headers[
            "X-Gateway-Cache"
        ] == "HIT"

        _age_entries(env, 100)
        # The default-window member still serves the same entry...
        assert (await _chat(env, steady, "shared question")).headers[
            "X-Gateway-Cache"
        ] == "HIT"
        # ...the 60-second member's read of it is a freshness miss.
        assert (await _chat(env, jumpy, "shared question")).headers[
            "X-Gateway-Cache"
        ] == "MISS"
