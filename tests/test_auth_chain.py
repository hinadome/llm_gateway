"""Tests for the pluggable authentication chain (auth.chain).

Built-in steps: ``admin_key``, ``virtual_key``, ``jwt``; custom steps load
from ``pkg.module:ClassName`` paths. The JWT step proves *identity* — the
key row named by the token's ``key_claim`` (default ``sub``) supplies the
*policy*, exactly as the raw virtual key would (budgets, limits, spend,
logs). Verification covers a shared HS256 secret and asymmetric keys from
a JWKS (cached, refreshed on unknown ``kid``).
"""

from __future__ import annotations

import json
import time

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import update

from gateway.config import AuthConfig, JWTAuthConfig
from gateway.core.auth import (
    AuthChain,
    Authenticator,
    auth_context_from_key,
    get_key_by_name,
)
from gateway.db import get_sessionmaker
from gateway.models import VirtualKey
from gateway.observability import metrics

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _key_row, _wait_for

SECRET = "test-shared-secret"
NOW = int(time.time())


def _hs256(claims: dict, secret: str = SECRET, headers: dict | None = None) -> str:
    return pyjwt.encode({**claims}, secret, algorithm="HS256", headers=headers)


def _jwt_config(**kwargs) -> AuthConfig:
    return AuthConfig(
        chain=["admin_key", "jwt", "virtual_key"],
        jwt=JWTAuthConfig(secret=SECRET, **kwargs),
    )


class _RsaKeyring:
    """An RSA keypair served as a JWKS over an injectable transport."""

    def __init__(self, kid: str = "key-1") -> None:
        self.requests = 0
        self.fail = False
        self.rotate(kid)

    def rotate(self, kid: str) -> None:
        """Swap the signing key (keeping fetch counters intact)."""
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = kid

    def jwk(self) -> dict:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk["kid"] = self.kid
        return jwk

    def token(self, claims: dict) -> str:
        return pyjwt.encode(
            {**claims}, self.private, algorithm="RS256", headers={"kid": self.kid}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.fail:
            return httpx.Response(503, json={"error": "idp down"})
        return httpx.Response(200, json={"keys": [self.jwk()]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# ----------------------------------------------------------------------
# Default chain: unchanged behavior
# ----------------------------------------------------------------------


async def test_default_chain_admin_and_virtual_key(tmp_path):
    async for env in _make_env(tmp_path):
        steps = [s.name for s in env.app.state.auth_chain.steps]
        assert steps == ["admin_key", "virtual_key"]

        admin = await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))
        assert admin.status_code == 200

        key = (await create_key(env.client, "plain-user"))["key"]
        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(key),
        )
        assert ok.status_code == 200

        # auth_method is recorded on the log row (written by the
        # background logging task)
        async def _row_logged() -> bool:
            logs = (
                await env.client.get(
                    "/admin/logs",
                    params={"key_name": "plain-user"},
                    headers=auth(ADMIN_KEY),
                )
            ).json()["data"]
            return bool(logs) and logs[0]["auth_method"] == "virtual_key"

        await _wait_for(_row_logged, what="log row")

        unknown = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth("sk-gw-not-a-real-key"),
        )
        assert unknown.status_code == 401
        break


async def test_admin_key_removed_from_chain(tmp_path):
    """A pure virtual-key chain: the admin key no longer authenticates —
    how you run the gateway when admins authenticate some other way."""
    config = make_config(auth=AuthConfig(chain=["virtual_key"]))
    async for env in _make_env(tmp_path, config=config):
        assert [s.name for s in env.app.state.auth_chain.steps] == ["virtual_key"]
        assert (
            await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))
        ).status_code == 401
        break


# ----------------------------------------------------------------------
# JWT step, HS256
# ----------------------------------------------------------------------


async def test_jwt_hs256_authenticates_and_applies_key_policy(tmp_path):
    config = make_config(auth=_jwt_config())
    async for env in _make_env(tmp_path, config=config):
        created = await create_key(
            env.client, "mobile-app", allowed_models=["test-*"], rpm_limit=100
        )
        token = _hs256({"sub": "mobile-app", "exp": NOW + 600})

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello from the token!"),
            headers=auth(token),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "test-model"

        # model policy from the key row applies to token requests
        denied = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model", content="no"),
            headers=auth(token),
        )
        assert denied.status_code == 404

        # auth_method lands on the log rows; spend accrues to the key
        # (both are written by the background logging task)
        async def _logged_with_method() -> bool:
            logs = (
                await env.client.get(
                    "/admin/logs",
                    params={"key_name": "mobile-app"},
                    headers=auth(ADMIN_KEY),
                )
            ).json()["data"]
            return bool(logs) and {row["auth_method"] for row in logs} == {"jwt"}

        await _wait_for(_logged_with_method, what="jwt log row")

        async def _spend_accrued() -> bool:
            row = await _key_row(env.client, created["id"])
            return row["spend_usd"] > 0

        await _wait_for(_spend_accrued, what="spend accrual")
        break


async def test_jwt_expired_and_leeway(tmp_path):
    config = make_config(auth=_jwt_config(leeway_seconds=30))
    async for env in _make_env(tmp_path, config=config):
        await create_key(env.client, "mobile-app")
        # 10s stale: within the clock-skew leeway
        stale = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "mobile-app", "exp": NOW - 10})),
        )
        assert stale.status_code == 200
        # well past it: rejected
        expired = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "mobile-app", "exp": NOW - 3600})),
        )
        assert expired.status_code == 401
        assert "Invalid JWT" in expired.json()["error"]["message"]
        # no exp at all: rejected (exp is always required)
        no_exp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "mobile-app"})),
        )
        assert no_exp.status_code == 401
        break


async def test_jwt_wrong_signature_and_bad_claims(tmp_path):
    config = make_config(auth=_jwt_config(issuer="https://idp.test"))
    async for env in _make_env(tmp_path, config=config):
        await create_key(env.client, "mobile-app")
        _ok = {"iss": "https://idp.test", "exp": NOW + 600}  # claims that pass verification

        wrong_secret = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(
                _hs256({"sub": "mobile-app", **_ok}, secret="nope")
            ),
        )
        assert wrong_secret.status_code == 401

        # verification passes, but sub names no virtual key
        ghost = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "ghost-team", **_ok})),
        )
        assert ghost.status_code == 401
        assert "ghost-team" in ghost.json()["error"]["message"]

        # no sub at all
        no_sub = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256(_ok)),
        )
        assert no_sub.status_code == 401

        # issuer not matching the configured one
        bad_iss = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(
                _hs256({"sub": "mobile-app", "exp": NOW + 600, "iss": "https://evil.test"})
            ),
        )
        assert bad_iss.status_code == 401

        # matching issuer passes
        good = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "mobile-app", **_ok})),
        )
        assert good.status_code == 200
        break


async def test_jwt_audience_and_key_claim_configurable(tmp_path):
    config = make_config(auth=_jwt_config(audience="llm-gateway", key_claim="azp"))
    async for env in _make_env(tmp_path, config=config):
        await create_key(env.client, "mobile-app")
        # key_claim is configurable: azp carries the key name here
        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(
                _hs256({"azp": "mobile-app", "exp": NOW + 600, "aud": "llm-gateway"})
            ),
        )
        assert ok.status_code == 200
        wrong_aud = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(
                _hs256({"azp": "mobile-app", "exp": NOW + 600, "aud": "other-api"})
            ),
        )
        assert wrong_aud.status_code == 401
        break


async def test_jwt_disabled_by_default(tmp_path):
    """Default chain has no jwt step: a perfectly valid token is just an
    unknown credential."""
    async for env in _make_env(tmp_path):
        await create_key(env.client, "mobile-app")
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "mobile-app", "exp": NOW + 600})),
        )
        assert resp.status_code == 401
        break


async def test_jwt_auth_metric_counts(tmp_path):
    config = make_config(auth=_jwt_config())
    async for env in _make_env(tmp_path, config=config):
        await create_key(env.client, "mobile-app")
        ok_before = metrics.AUTH.labels(
            authenticator="jwt", result="authenticated"
        )._value.get()
        rejected_before = metrics.AUTH.labels(
            authenticator="jwt", result="rejected"
        )._value.get()

        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "mobile-app", "exp": NOW + 600})),
        )
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(_hs256({"sub": "ghost", "exp": NOW + 600})),
        )
        assert (
            metrics.AUTH.labels(authenticator="jwt", result="authenticated")._value.get()
            == ok_before + 1
        )
        assert (
            metrics.AUTH.labels(authenticator="jwt", result="rejected")._value.get()
            == rejected_before + 1
        )
        break


async def test_jwt_names_admin_virtual_key(tmp_path):
    """A JWT naming an *admin* virtual key carries its admin rights."""
    config = make_config(auth=_jwt_config())
    async for env in _make_env(tmp_path, config=config):
        await create_key(env.client, "break-glass")
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VirtualKey)
                .where(VirtualKey.name == "break-glass")
                .values(is_admin=True)
            )
            await session.commit()

        admin = await env.client.get(
            "/admin/keys", headers=auth(_hs256({"sub": "break-glass", "exp": NOW + 600}))
        )
        assert admin.status_code == 200
        assert admin.json()["data"][0]["name"] == "break-glass"
        break


# ----------------------------------------------------------------------
# JWT step, RS256 via JWKS
# ----------------------------------------------------------------------


async def test_jwt_rs256_via_jwks(tmp_path):
    ring = _RsaKeyring()
    config = make_config(
        auth=AuthConfig(
            chain=["admin_key", "jwt"],
            jwt=JWTAuthConfig(
                jwks_url="https://idp.test/.well-known/jwks.json",
                jwks_cache_seconds=300,
            ),
        )
    )
    async for env in _make_env(tmp_path, config=config, jwks_client=ring.client()):
        await create_key(env.client, "mobile-app")
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(ring.token({"sub": "mobile-app", "exp": NOW + 600})),
        )
        assert resp.status_code == 200
        assert ring.requests == 1  # fetched once, then cached

        # a second request within the cache TTL does not re-fetch
        await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi again"),
            headers=auth(ring.token({"sub": "mobile-app", "exp": NOW + 600})),
        )
        assert ring.requests == 1
        break


async def test_jwks_rotation_refreshes_on_unknown_kid(tmp_path):
    """An unknown kid triggers one refresh — key rotation works without
    waiting out the cache TTL; the old key stops verifying."""
    ring = _RsaKeyring(kid="key-1")
    config = make_config(
        auth=AuthConfig(
            chain=["admin_key", "jwt"],
            jwt=JWTAuthConfig(jwks_url="https://idp.test/jwks", jwks_cache_seconds=300),
        )
    )
    async for env in _make_env(tmp_path, config=config, jwks_client=ring.client()):
        await create_key(env.client, "mobile-app")
        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(ring.token({"sub": "mobile-app", "exp": NOW + 600})),
        )
        assert first.status_code == 200
        assert ring.requests == 1

        # the provider rotates its signing key (and drops the old one)
        old_ring = _RsaKeyring(kid="key-1")
        ring.rotate("key-2")
        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(ring.token({"sub": "mobile-app", "exp": NOW + 600})),
        )
        assert second.status_code == 200
        assert ring.requests == 2  # cached fetch + rotation refresh

        # a token signed by the *old* (now unlisted) key fails
        stale = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(old_ring.token({"sub": "mobile-app", "exp": NOW + 600})),
        )
        assert stale.status_code == 401
        break


async def test_jwks_unreachable_is_503(tmp_path):
    """An unreachable identity provider fails closed, but with an honest
    503 rather than a 401 blaming the client."""
    ring = _RsaKeyring()
    ring.fail = True
    config = make_config(
        auth=AuthConfig(
            chain=["admin_key", "jwt"],
            jwt=JWTAuthConfig(jwks_url="https://idp.test/jwks"),
        )
    )
    async for env in _make_env(tmp_path, config=config, jwks_client=ring.client()):
        await create_key(env.client, "mobile-app")
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth(ring.token({"sub": "mobile-app", "exp": NOW + 600})),
        )
        assert resp.status_code == 503
        assert "JWKS" in resp.json()["error"]["message"]
        break


# ----------------------------------------------------------------------
# Custom steps + config validation
# ----------------------------------------------------------------------


class StaticTokenAuthenticator(Authenticator):
    """Test-only custom step: one magic token, policies from a key row."""

    name = "static_token"

    def __init__(self, config=None) -> None:  # noqa: ARG002 - chain contract
        self._token = "magic-opener"

    async def authenticate(self, credential: str):
        if credential != self._token:
            return None
        vkey = await get_key_by_name("mobile-app")
        if vkey is None:
            return None
        return auth_context_from_key(vkey, auth_method=self.name)


async def test_custom_authenticator_in_chain(tmp_path):
    config = make_config(
        auth=AuthConfig(
            chain=[f"{__name__}:StaticTokenAuthenticator", "admin_key"]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        await create_key(env.client, "mobile-app")
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hi"),
            headers=auth("magic-opener"),
        )
        assert resp.status_code == 200
        # the custom step's name is what gets recorded
        async def _row_logged() -> bool:
            logs = (
                await env.client.get(
                    "/admin/logs",
                    params={"key_name": "mobile-app"},
                    headers=auth(ADMIN_KEY),
                )
            ).json()["data"]
            return bool(logs) and logs[0]["auth_method"] == "static_token"

        await _wait_for(_row_logged, what="custom-step log row")
        break


def test_invalid_chain_entries_fail_at_construction():
    with pytest.raises(ValueError, match="Unknown authenticator"):
        AuthChain.from_config(AuthConfig(chain=["sso"]), admin_key_provider=lambda: None)
    with pytest.raises(ValueError, match="at least one"):
        AuthChain.from_config(AuthConfig(chain=[]), admin_key_provider=lambda: None)
    with pytest.raises(ValueError, match="needs a shared 'secret'"):
        AuthChain.from_config(AuthConfig(chain=["jwt"]), admin_key_provider=lambda: None)
    with pytest.raises(ValueError, match="unsupported"):
        AuthChain.from_config(
            AuthConfig(chain=["jwt"], jwt=JWTAuthConfig(secret="s", algorithms=["none"])),
            admin_key_provider=lambda: None,
        )
    with pytest.raises(TypeError, match="not an Authenticator"):
        AuthChain.from_config(
            AuthConfig(chain=["os.path:join"]), admin_key_provider=lambda: None
        )
