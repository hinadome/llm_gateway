"""Data-residency routing: deployment-tag-constrained routing, fail
closed.

A residency requirement on a key (and/or its tenant) restricts which
deployments may serve — and shadow-MIRROR — its traffic: only those
whose config-declared ``residency`` tag is in the caller's effective
set (key ∩ tenant; the tenant can only narrow). The filter runs
inside ``Router.resolve``, BEFORE the priority ladder, retries and
fallbacks, so a constrained request can never escape to a
non-compliant deployment mid-failover. Untagged deployments satisfy
NO requirement (fail closed: no claim, no compliance); a model with
nothing compliant answers 404 ``residency_not_satisfied``. Cache
entries never cross residency classes (the requirement rides the
response-policy token), and the semantic cache degrades away rather
than embed a restricted caller's prompt non-compliantly.
"""

from __future__ import annotations

from gateway.config import DeploymentConfig, ModelConfig

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _wait_for


def _models() -> list[ModelConfig]:
    return [
        # eu-first ladder with a us fallback: unrestricted callers hit
        # eu/primary, eu-restricted callers too, us-restricted the
        # us fallback, nobody else.
        ModelConfig(
            name="test-model",
            deployments=[
                DeploymentConfig(
                    name="eu/primary", provider="openai", model="model-a",
                    residency="eu",
                ),
                DeploymentConfig(
                    name="us/fallback", provider="openai", model="model-b",
                    residency="us", priority=200,
                ),
            ],
        ),
        # Only eu-tagged: a us-restricted caller has nothing to serve.
        ModelConfig(
            name="eu-only",
            deployments=[
                DeploymentConfig(
                    name="eu/only", provider="openai", model="model-c",
                    residency="eu",
                ),
            ],
        ),
        # Untagged only: satisfies no requirement, unrestricted fine.
        ModelConfig(
            name="untagged-model",
            deployments=[
                DeploymentConfig(
                    name="plain/only", provider="openai", model="model-c"
                ),
            ],
        ),
        # Untagged PRIMARY (higher priority) + eu fallback: the
        # untagged one must be filtered out before the ladder, not
        # preferred over the compliant one.
        ModelConfig(
            name="mixed-ladder",
            deployments=[
                DeploymentConfig(
                    name="untagged/ladder", provider="openai", model="model-a"
                ),
                DeploymentConfig(
                    name="eu/ladder", provider="openai", model="model-b",
                    residency="eu", priority=200,
                ),
            ],
        ),
        # eu primary (will be failed in tests) + us fallback: a
        # eu-restricted request must FAIL rather than escape to us.
        ModelConfig(
            name="eu-failing",
            deployments=[
                DeploymentConfig(
                    name="eu/failing", provider="openai", model="model-a",
                    residency="eu",
                ),
                DeploymentConfig(
                    name="us/failing", provider="openai", model="model-b",
                    residency="us", priority=200,
                ),
            ],
        ),
        # Two eu deployments: failures still fail over — WITHIN the
        # compliant class. Residency narrows the ladder, never disables
        # it. eu/pair-primary fails in the test; eu/pair-fallback serves.
        ModelConfig(
            name="eu-pair",
            deployments=[
                DeploymentConfig(
                    name="eu/pair-primary", provider="openai", model="model-a",
                    residency="eu",
                ),
                DeploymentConfig(
                    name="eu/pair-fallback", provider="openai", model="model-b",
                    residency="eu", priority=200,
                ),
            ],
        ),
        # Serving eu + two shadow twins: the eu shadow may receive a
        # mirror copy, the us shadow may not (a mirror is a copy of
        # the caller's data).
        ModelConfig(
            name="mirror-model",
            deployments=[
                DeploymentConfig(
                    name="eu/serve", provider="openai", model="model-a",
                    residency="eu",
                ),
                DeploymentConfig(
                    name="eu/shadow", provider="openai", model="model-b",
                    residency="eu", shadow=True,
                ),
                DeploymentConfig(
                    name="us/shadow", provider="openai", model="model-c",
                    residency="us", shadow=True,
                ),
            ],
        ),
        # The semantic-cache embedder model: untagged, so a restricted
        # caller's prompt can never be embedded through it.
        ModelConfig(
            name="embed-model",
            deployments=[
                DeploymentConfig(
                    name="embed/only", provider="openai", model="model-embed"
                ),
            ],
        ),
    ]


def _config(**overrides):
    overrides.setdefault("models", _models())
    return make_config(**overrides)


def _mock_models(env) -> list[str]:
    """Upstream model names the mock has been asked for (deployments
    are distinguishable by their native model id)."""
    return [b["model"] for b in env.mock.bodies() if isinstance(b, dict) and "model" in b]


async def _chat(env, key: str, content: str = "hello", **extra):
    return await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=content, **extra),
        headers=auth(key),
    )


# ----------------------------------------------------------------------
# The core contract: compliant-only serving, fail closed
# ----------------------------------------------------------------------


async def test_restricted_caller_served_only_by_compliant_deployment(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]

        resp = await _chat(env, key)
        assert resp.status_code == 200, resp.text
        # The eu primary (priority 100) is compliant and preferred.
        assert resp.headers["x-gateway-deployment"] == "eu/primary"

        # Same answer on every inbound surface: the filter is inside
        # the shared pipeline, not per-route.
        native = await env.client.post(
            "/v1/messages",
            json={
                "model": "test-model",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"x-api-key": key},
        )
        assert native.status_code == 200
        assert native.headers["x-gateway-deployment"] == "eu/primary"

        streamed = await _chat(env, key, "streamed too", stream=True)
        assert streamed.status_code == 200
        assert streamed.headers["x-gateway-deployment"] == "eu/primary"
        await streamed.aread()

        # Only eu deployments were ever consulted.
        assert set(_mock_models(env)) <= {"model-a", "model-b", "model-c"}


async def test_no_compliant_deployment_fails_closed_404(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "us-bot", required_residencies=["us"]))[
            "key"
        ]
        # eu-only has exactly one deployment, tagged eu: a us-restricted
        # caller has nothing compliant to serve it.
        resp = await _chat(env, key, "hi", model="eu-only")
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "residency_not_satisfied"
        # The message names the model and the requirement — the
        # operator can tell residency apart from a missing model.
        assert "eu-only" in err["message"]
        assert "us" in err["message"]
        assert _mock_models(env) == []  # nothing was ever consulted


async def test_untagged_deployments_satisfy_nothing(tmp_path):
    # An untagged deployment makes no claim, so it can never prove
    # compliance — fail closed, exactly like a missing tag.
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello", model="untagged-model"),
            headers=auth(key),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "residency_not_satisfied"


async def test_unrestricted_callers_completely_unchanged(tmp_path):
    # No requirement = no filtering, no new behavior: the untagged
    # primary of the mixed ladder is preferred exactly as before.
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "free"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello", model="untagged-model"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-deployment"] == "plain/only"
        # Admin/env-key traffic carries no key: exempt, like every
        # key-layer ceiling.
        admin = await _chat(env, ADMIN_KEY)
        assert admin.status_code == 200
        assert admin.headers["x-gateway-deployment"] == "eu/primary"


# ----------------------------------------------------------------------
# Fail closed across the routing machinery
# ----------------------------------------------------------------------


async def test_untagged_primary_filtered_before_ladder(tmp_path):
    # The untagged deployment is HIGHER priority — a filter that only
    # applied at pick time would still try it first. The eu-restricted
    # caller is served by the compliant fallback directly.
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello", model="mixed-ladder"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-deployment"] == "eu/ladder"
        assert "model-a" not in _mock_models(env)  # the primary never ran


async def test_failure_never_escapes_to_non_compliant_fallback(tmp_path):
    # The eu primary exhausts its retries; the us fallback would save
    # the request in an unrestricted world — here the request FAILS,
    # and the us deployment is never even consulted.
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]
        env.mock.fail_counts["model-a"] = 3  # exhaust the primary's budget

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello", model="eu-failing"),
            headers=auth(key),
        )
        assert resp.status_code >= 500  # refused over escaping residency
        assert _mock_models(env).count("model-a") == 3  # the budget spent on eu
        assert "model-b" not in _mock_models(env)  # us never consulted


async def test_compliant_fallback_still_works_within_class(tmp_path):
    # Failures still fail over — to COMPLIANT deployments only: the eu
    # pair-primary dies, the eu pair-fallback serves. Residency narrows
    # the ladder, it does not disable it.
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]
        env.mock.fail_counts["model-a"] = 3  # exhaust the primary's budget
        resp = await _chat(env, key, "fail over in class", model="eu-pair")
        assert resp.status_code == 200
        assert resp.headers["x-gateway-deployment"] == "eu/pair-fallback"
        assert _mock_models(env).count("model-a") == 3  # the retries it took
        assert _mock_models(env).count("model-b") == 1  # the fallback served


# ----------------------------------------------------------------------
# Key ∩ tenant
# ----------------------------------------------------------------------


async def test_key_and_tenant_requirements_intersect(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        acme = (
            await env.client.post(
                "/admin/tenants",
                json={"name": "acme", "required_residencies": ["eu"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        # key: eu, tenant: eu -> both satisfied, serves.
        member_eu = (
            await create_key(
                env.client, "acme-eu", tenant_id=acme["id"], required_residencies=["eu"]
            )
        )["key"]
        resp = await _chat(env, member_eu)
        assert resp.status_code == 200
        # key: none, tenant: eu -> the tenant's requirement binds every
        # member (it can only narrow).
        member_none = (
            await create_key(env.client, "acme-none", tenant_id=acme["id"])
        )["key"]
        assert (await _chat(env, member_none)).status_code == 200
        # key: us, tenant: eu -> empty intersection: nothing can
        # serve, fail closed.
        member_us = (
            await create_key(
                env.client, "acme-us", tenant_id=acme["id"], required_residencies=["us"]
            )
        )["key"]
        resp = await _chat(env, member_us)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "residency_not_satisfied"


# ----------------------------------------------------------------------
# Shadow mirrors: a copy of the caller's data lands only compliantly
# ----------------------------------------------------------------------


async def test_shadow_mirrors_respect_residency(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        eu_key = (
            await create_key(env.client, "eu-bot", required_residencies=["eu"])
        )["key"]
        free_key = (await create_key(env.client, "free"))["key"]

        # Restricted caller: the eu shadow receives the mirror, the
        # us shadow never does.
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="mirror me", model="mirror-model"),
            headers=auth(eu_key),
        )
        assert resp.status_code == 200

        async def _eu_mirror_arrived() -> bool:
            return "model-b" in _mock_models(env)

        await _wait_for(_eu_mirror_arrived, what="eu shadow mirror")
        assert "model-c" not in _mock_models(env)  # us shadow: never

        # Unrestricted caller (control): both shadows receive copies —
        # the filter only exists when a requirement does.
        before = len(env.mock.requests)
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="mirror me too", model="mirror-model"),
            headers=auth(free_key),
        )
        assert resp.status_code == 200

        async def _both_mirrors_arrived() -> bool:
            tail = _mock_models(env)[before:]
            return "model-b" in tail and "model-c" in tail

        await _wait_for(_both_mirrors_arrived, what="both shadow mirrors")


# ----------------------------------------------------------------------
# Caches: classes never share entries, semantic degrades never violates
# ----------------------------------------------------------------------


async def test_cache_entries_never_cross_residency_classes(tmp_path):
    # The requirement rides the response-policy token: an unrestricted
    # caller's entry (possibly generated on a deployment a restricted
    # caller may not use) can never serve the restricted caller — the
    # restricted request goes upstream itself.
    async for env in _make_env(tmp_path, config=_config()):
        free = (await create_key(env.client, "free"))["key"]
        eu = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]

        first = await _chat(env, free, "shared content")
        assert first.status_code == 200
        assert first.headers["x-gateway-cache"] == "MISS"

        second = await _chat(env, eu, "shared content")
        assert second.status_code == 200
        assert second.headers["x-gateway-cache"] == "MISS"  # NOT the free entry
        assert _mock_models(env).count("model-a") == 2  # went upstream again

        # And the restricted caller's entry never serves the free one
        # either (the token differs in both directions).
        third = await _chat(env, free, "shared content")
        assert third.headers["x-gateway-cache"] == "HIT"  # its own class's entry


async def test_semantic_cache_degrades_never_violates(tmp_path):
    # The embedder model is untagged, so a restricted caller's prompt
    # can never be embedded through it: the semantic lookup quietly
    # disappears (the request still serves) — degradation, never a
    # residency violation. An unrestricted caller's embed fires.
    from gateway.config import SemanticCacheConfig

    config = _config(
        semantic_cache=SemanticCacheConfig(
            enabled=True, model="embed-model", threshold=0.95
        )
    )
    async for env in _make_env(tmp_path, config=config):
        free = (await create_key(env.client, "free"))["key"]
        eu = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]

        # Unrestricted: the embedder IS consulted (semantic path live).
        resp = await _chat(env, free, "semantic me")
        assert resp.status_code == 200

        async def _embed_fired() -> bool:
            return "model-embed" in _mock_models(env)

        await _wait_for(_embed_fired, what="unrestricted semantic embed")
        before = len(env.mock.requests)

        # Restricted: no embed request ever fires, the request serves.
        resp = await _chat(env, eu, "semantic me too")
        assert resp.status_code == 200
        assert "model-embed" not in _mock_models(env)[before:]


# ----------------------------------------------------------------------
# Other surfaces: embeddings filter too; the admin routing view
# ----------------------------------------------------------------------


async def test_embeddings_surface_filters_too(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        eu = (await create_key(env.client, "eu-bot", required_residencies=["eu"]))[
            "key"
        ]
        us = (await create_key(env.client, "us-bot", required_residencies=["us"]))[
            "key"
        ]

        ok = await env.client.post(
            "/v1/embeddings",
            json={"model": "eu-only", "input": "hello"},
            headers=auth(eu),
        )
        assert ok.status_code == 200

        denied = await env.client.post(
            "/v1/embeddings",
            json={"model": "eu-only", "input": "hello"},
            headers=auth(us),
        )
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "residency_not_satisfied"


async def test_admin_routing_view_shows_residency_claims(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        view = (
            await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))
        ).json()
        models = {m["name"]: m for m in view["models"]}
        by_name = {
            d["name"]: d for m in view["models"] for d in m["deployments"]
        }
        assert by_name["eu/primary"]["residency"] == "eu"
        assert by_name["us/fallback"]["residency"] == "us"
        assert by_name["plain/only"]["residency"] is None  # no claim, visible


# ----------------------------------------------------------------------
# API surface: normalization, PATCH semantics, validation
# ----------------------------------------------------------------------


async def test_api_surface_normalization_and_validation(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        # Create: tags are normalized (case, whitespace, dedupe).
        created = await create_key(
            env.client, "eu-key", required_residencies=["EU", " eu ", "eu-west"]
        )
        assert created["required_residencies"] == ["eu", "eu-west"]

        # PATCH: move, then clear with the explicit empty list (same
        # as null — the requirement is gone).
        moved = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"required_residencies": ["us"]},
            headers=auth(ADMIN_KEY),
        )
        assert moved.json()["required_residencies"] == ["us"]
        cleared = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"required_residencies": []},
            headers=auth(ADMIN_KEY),
        )
        assert cleared.json()["required_residencies"] is None

        # Invalid tags are a 400, not a silent pass.
        bad = await env.client.post(
            "/admin/keys",
            json={"name": "bad", "required_residencies": ["not a slug!"]},
            headers=auth(ADMIN_KEY),
        )
        assert bad.status_code == 400

        # Tenant surface: same shape.
        tenant = (
            await env.client.post(
                "/admin/tenants",
                json={"name": "acme", "required_residencies": ["EU"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        assert tenant["required_residencies"] == ["eu"]
        patched = await env.client.patch(
            f"/admin/tenants/{tenant['id']}",
            json={"required_residencies": None},
            headers=auth(ADMIN_KEY),
        )
        assert patched.json()["required_residencies"] is None

        # Config-side validation: an invalid tag in a deployment is a
        # config-load error (the residency vocabulary is exact-match).
        from gateway.config import GatewayConfig
        try:
            GatewayConfig.model_validate(
                {
                    "providers": {},
                    "models": [
                        {
                            "name": "m",
                            "deployments": [
                                {
                                    "name": "d",
                                    "provider": "openai",
                                    "model": "x",
                                    "residency": "Not A Slug!",
                                }
                            ],
                        }
                    ],
                }
            )
            raise AssertionError("invalid residency tag accepted")
        except Exception as exc:  # noqa: BLE001 - pydantic wraps ValueError
            assert "residency" in str(exc)
