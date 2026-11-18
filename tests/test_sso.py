"""Dashboard SSO (tier 60): the OIDC authorization-code + PKCE login.

The mock IdP below serves discovery, a JWKS and the token endpoint over
an injectable transport (the authorization endpoint is only ever a
redirect target — the browser would visit it, tests never do). RSA
keypairs per instance, exactly like ``_RsaKeyring`` in test_auth_chain.

The flow under test, end to end: login 302 (PKCE S256 + state in an
HttpOnly cookie) → the IdP redirects back to /admin/ui/sso/callback →
code exchange + ID-token verification against the JWKS + the email
allowlist → a gateway-minted HS256 session cookie → POST
/admin/ui/sso/session exchanges it once for the Bearer the admin API
speaks. The browser never holds the raw admin key; the audit trail
records the signed-in human, not a shared key.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from gateway.config import AuditConfig, AuthConfig, JWTAuthConfig, SSOConfig
from gateway.core.sso import SESSION_AUDIENCE, SESSION_ISSUER

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_audit_webhooks import AuditSink, _wait_for

ISSUER = "https://idp.test"
CLIENT_ID = "gw-admin"
REDIRECT_URI = "https://gw.test/admin/ui/sso/callback"
KID = "sso-k1"
STATE_COOKIE = "gw_sso_state"
SESSION_COOKIE = "gw_sso_session"
SESSION_SECRET_ENV = "GATEWAY_SSO_SESSION_SECRET"
CLIENT_SECRET_ENV = "SSO_CLIENT_SECRET"
SESSION_SECRET = "test-session-secret-0123456789abcdef"
CLIENT_SECRET = "idp-secret"


class MockIdP:
    """A miniature OIDC provider: discovery, JWKS, token endpoint.

    ``paths`` records every request seen (the explicit-endpoints test
    proves discovery was skipped); ``token_forms`` the parsed form of
    every code exchange (the PKCE/secret assertions).
    """

    def __init__(
        self,
        email: str = "alice@example.com",
        email_verified: bool = True,
    ) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.email = email
        self.email_verified = email_verified
        self.paths: list[str] = []
        self.token_forms: list[dict] = []

    def jwk(self) -> dict:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
        return jwk

    def id_token(self, sub: str = "user-1", ttl: int = 300) -> str:
        now = int(time.time())
        return pyjwt.encode(
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": sub,
                "email": self.email,
                "email_verified": self.email_verified,
                "iat": now,
                "exp": now + ttl,
            },
            self.private,
            algorithm="RS256",
            headers={"kid": KID},
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if path == "/token":
            self.token_forms.append(urllib.parse.parse_qs(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "access_token": "mock-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "id_token": self.id_token(),
                },
            )
        return httpx.Response(404, text=f"mock IdP has no {path}")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _sso_cfg(**overrides) -> SSOConfig:
    defaults = dict(
        enabled=True,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret_env=CLIENT_SECRET_ENV,
        redirect_uri=REDIRECT_URI,
        allowed_domains=["example.com"],
    )
    return SSOConfig(**{**defaults, **overrides})


def _sso_auth(**overrides) -> AuthConfig:
    return AuthConfig(sso=_sso_cfg(**overrides))


async def _sso_env(
    tmp_path,
    monkeypatch,
    auth: AuthConfig | None = None,
    idp: MockIdP | None = None,
    config=None,
    jwks_too: bool = False,
    **env_kwargs,
):
    """An env with SSO enabled against a fresh mock IdP. Both secrets
    are set (confidential client by default); ``jwks_too`` shares the
    same IdP client with the chain's jwt step (the fall-through test).
    """
    idp = idp if idp is not None else MockIdP()
    monkeypatch.setenv(SESSION_SECRET_ENV, SESSION_SECRET)
    monkeypatch.setenv(CLIENT_SECRET_ENV, CLIENT_SECRET)
    sso_client = httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    cfg = config if config is not None else make_config(auth=auth or _sso_auth())
    extras = {"jwks_client": sso_client} if jwks_too else {}
    try:
        async for ns in _make_env(
            tmp_path,
            config=cfg,
            sso_http_client=sso_client,
            **extras,
            **env_kwargs,
        ):
            ns.idp = idp
            yield ns
            break
    finally:
        await sso_client.aclose()


async def _login_flow(client: httpx.AsyncClient) -> dict:
    """login 302 → callback → ?sso=ok → cookie exchanged for the Bearer.

    Returns the session-exchange body (token + email). The state cookie
    travels in the client's jar, exactly as it would in a browser.
    """
    login = await client.get("/admin/ui/sso/login")
    assert login.status_code == 302, login.text
    state = httpx.URL(login.headers["location"]).params["state"]
    cb = await client.get(
        "/admin/ui/sso/callback", params={"code": "authz-1", "state": state}
    )
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/admin/ui?sso=ok", cb.headers["location"]
    ex = await client.post("/admin/ui/sso/session")
    assert ex.status_code == 200, ex.text
    return ex.json()


async def _login_error_location(client: httpx.AsyncClient) -> str:
    """Drive the flow but return the callback's error redirect (the
    callback always 302s — landing on the dashboard with a reason)."""
    login = await client.get("/admin/ui/sso/login")
    assert login.status_code == 302, login.text
    state = httpx.URL(login.headers["location"]).params["state"]
    cb = await client.get(
        "/admin/ui/sso/callback", params={"code": "authz-1", "state": state}
    )
    assert cb.status_code == 302, cb.text
    return cb.headers["location"]


# ----------------------------------------------------------------------
# Disabled by default: honest answers, no half-configured routes
# ----------------------------------------------------------------------


async def test_disabled_sso_answers_honestly(tmp_path):
    async for env in _make_env(tmp_path):
        probe = await env.client.get("/admin/ui/sso/config")
        assert probe.status_code == 200
        assert probe.json() == {"enabled": False, "label": None}
        for method, path in (
            ("GET", "/admin/ui/sso/login"),
            ("GET", "/admin/ui/sso/callback"),
            ("POST", "/admin/ui/sso/session"),
            ("POST", "/admin/ui/sso/logout"),
        ):
            resp = await env.client.request(method, path)
            assert resp.status_code == 404, (method, path, resp.text)
            assert resp.json()["error"]["code"] == "sso_disabled"
        break


# ----------------------------------------------------------------------
# The happy path, with every OAuth moving part asserted
# ----------------------------------------------------------------------


async def test_full_happy_path(tmp_path, monkeypatch):
    idp = MockIdP()
    async for env in _sso_env(
        tmp_path, monkeypatch, auth=_sso_auth(label="Acme SSO"), idp=idp
    ):
        client = env.client

        # The dashboard's probe: button visibility + label.
        probe = await client.get("/admin/ui/sso/config")
        assert probe.json() == {"enabled": True, "label": "Acme SSO"}

        # Login: a 302 to the provider, PKCE S256 + state, and the
        # verifier NEVER in the URL — only in the HttpOnly cookie.
        login = await client.get("/admin/ui/sso/login")
        assert login.status_code == 302
        loc = httpx.URL(login.headers["location"])
        assert str(loc).startswith(f"{ISSUER}/authorize?")
        q = dict(loc.params)
        assert q["response_type"] == "code"
        assert q["client_id"] == CLIENT_ID
        assert q["redirect_uri"] == REDIRECT_URI
        assert q["scope"] == "openid email"
        assert q["code_challenge_method"] == "S256"
        assert len(q["code_challenge"]) >= 43  # SHA-256, base64url
        carried_state, verifier = client.cookies.get(STATE_COOKIE).split(":")
        assert carried_state == q["state"]
        # the PKCE verifier is HttpOnly-cookie-carried, never in the URL
        assert verifier not in str(loc)
        assert len(verifier) >= 40

        # Callback: exchange + verify + allowlist → session cookie.
        cb = await client.get(
            "/admin/ui/sso/callback",
            params={"code": "authz-1", "state": q["state"]},
        )
        assert cb.status_code == 302
        assert cb.headers["location"] == "/admin/ui?sso=ok"
        assert client.cookies.get(SESSION_COOKIE) is not None

        # The exchange hit the token endpoint exactly once, with the
        # cookie's verifier, the right grant, and the client secret in
        # the POST body (never the URL).
        (form,) = idp.token_forms
        assert form["grant_type"] == ["authorization_code"]
        assert form["code"] == ["authz-1"]
        assert form["code_verifier"] == [verifier]
        assert form["client_id"] == [CLIENT_ID]
        assert form["redirect_uri"] == [REDIRECT_URI]
        assert form["client_secret"] == [CLIENT_SECRET]

        # Cookie → Bearer: the dashboard's one-time exchange.
        ex = await client.post("/admin/ui/sso/session")
        assert ex.status_code == 200
        body = ex.json()
        assert body["email"] == "alice@example.com"
        assert body["expires_in"] == 900
        assert body["token"] == client.cookies.get(SESSION_COOKIE)

        # The minted session authenticates on the admin API.
        keys = await client.get(
            "/admin/keys", headers={"Authorization": f"Bearer {body['token']}"}
        )
        assert keys.status_code == 200
        break


# ----------------------------------------------------------------------
# Callback refusals — every failure lands with an honest reason
# ----------------------------------------------------------------------


async def test_state_mismatch_is_refused(tmp_path, monkeypatch):
    # The login did not originate in this browser: the code must never
    # be exchanged (no redemption of a stolen authorization code).
    idp = MockIdP()
    async for env in _sso_env(tmp_path, monkeypatch, idp=idp):
        login = await env.client.get("/admin/ui/sso/login")
        assert login.status_code == 302
        cb = await env.client.get(
            "/admin/ui/sso/callback",
            params={"code": "authz-1", "state": "not-the-state"},
        )
        assert cb.status_code == 302
        assert (
            cb.headers["location"] == "/admin/ui?sso=error&reason=state_mismatch"
        )
        assert idp.token_forms == []  # never exchanged
        assert env.client.cookies.get(SESSION_COOKIE) is None
        break


async def test_callback_without_login_cookie(tmp_path, monkeypatch):
    # A direct callback hit with no state cookie at all.
    async for env in _sso_env(tmp_path, monkeypatch):
        cb = await env.client.get(
            "/admin/ui/sso/callback",
            params={"code": "authz-1", "state": "whatever"},
        )
        assert cb.status_code == 302
        assert (
            cb.headers["location"]
            == "/admin/ui?sso=error&reason=missing_code_or_state"
        )
        break


async def test_allowlist_gates_sign_in(tmp_path, monkeypatch):
    # Outside both the allowed domains and allowed emails.
    idp = MockIdP(email="mallory@evil.test")
    async for env in _sso_env(tmp_path, monkeypatch, idp=idp):
        loc = await _login_error_location(env.client)
        assert "reason=not_allowed" in loc
        assert "mallory" in loc  # the message names the refused email
        assert env.client.cookies.get(SESSION_COOKIE) is None
        break


async def test_allowed_emails_exact_match_is_case_insensitive(tmp_path, monkeypatch):
    # Exact-email allowlist, no domain rule: mixed case still matches,
    # and the minted subject is the lowercased address.
    idp = MockIdP(email="Alice@Example.com")
    auth = _sso_auth(allowed_emails=["alice@example.com"], allowed_domains=[])
    async for env in _sso_env(tmp_path, monkeypatch, auth=auth, idp=idp):
        body = await _login_flow(env.client)
        assert body["email"] == "alice@example.com"
        break


async def test_unverified_email_refused(tmp_path, monkeypatch):
    # email_verified: false means the address is not the caller's — an
    # allowlist match on it would be a hole.
    idp = MockIdP(email_verified=False)
    async for env in _sso_env(tmp_path, monkeypatch, idp=idp):
        loc = await _login_error_location(env.client)
        assert "reason=email_not_verified" in loc
        assert env.client.cookies.get(SESSION_COOKIE) is None
        break


# ----------------------------------------------------------------------
# Session tokens: stateless, short-lived, honestly rejected
# ----------------------------------------------------------------------


async def test_expired_and_forged_sessions_rejected(tmp_path, monkeypatch):
    async for env in _sso_env(tmp_path, monkeypatch):
        client = env.client
        now = int(time.time())
        expired = pyjwt.encode(
            {
                "iss": SESSION_ISSUER,
                "aud": SESSION_AUDIENCE,
                "sub": "alice@example.com",
                "iat": now - 1000,
                "exp": now - 10,
            },
            SESSION_SECRET,
            algorithm="HS256",
        )
        forged = pyjwt.encode(
            {
                "iss": SESSION_ISSUER,
                "aud": SESSION_AUDIENCE,
                "sub": "alice@example.com",
                "iat": now,
                "exp": now + 900,
            },
            "not-the-session-secret",
            algorithm="HS256",
        )
        for token in (expired, forged):
            resp = await client.get(
                "/admin/keys", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401
        # The exchange route is equally honest about a dead cookie.
        client.cookies.set(
            SESSION_COOKIE, expired, domain="gateway.test", path="/admin/ui"
        )
        resp = await client.post("/admin/ui/sso/session")
        assert resp.status_code == 401
        break


async def test_foreign_jwt_falls_through_to_the_jwt_step(tmp_path, monkeypatch):
    """The iss peek in action: with SSO enabled AND a jwt step in the
    chain, an IdP-minted token (iss = the IdP) must pass sso_session
    untouched and authenticate via jwt — the auto-insert order may
    never swallow foreign tokens."""
    idp = MockIdP()
    auth = AuthConfig(
        chain=["admin_key", "virtual_key", "jwt"],
        jwt=JWTAuthConfig(
            jwks_url=f"{ISSUER}/jwks", issuer=ISSUER, audience=CLIENT_ID
        ),
        sso=_sso_cfg(),
    )
    async for env in _sso_env(
        tmp_path, monkeypatch, auth=auth, idp=idp, jwks_too=True
    ):
        # The foreign token's sub names this virtual key.
        await create_key(env.client, "user-1")

        # Foreign JWT: sso_session declines it (iss is the IdP's), jwt
        # verifies it against the same JWKS → the key's own principal,
        # a non-admin. 403 = authenticated but not admin (a swallowed
        # token would have been a 401 with our session message).
        resp = await env.client.get(
            "/admin/keys", headers={"Authorization": f"Bearer {idp.id_token()}"}
        )
        assert resp.status_code == 403, resp.text

        # A claimed-but-wrong session token is still a positive reject
        # — the honest 401, not a fall-through.
        resp = await env.client.get(
            "/admin/keys",
            headers={"Authorization": "Bearer not-a-jwt-at-all"},
        )
        assert resp.status_code == 401

        # And the gateway session in the same env is an admin.
        body = await _login_flow(env.client)
        resp = await env.client.get(
            "/admin/keys", headers={"Authorization": f"Bearer {body['token']}"}
        )
        assert resp.status_code == 200
        break


# ----------------------------------------------------------------------
# Client shapes + wiring details
# ----------------------------------------------------------------------


async def test_public_client_sends_no_secret(tmp_path, monkeypatch):
    idp = MockIdP()
    async for env in _sso_env(
        tmp_path, monkeypatch, auth=_sso_auth(client_secret_env=None), idp=idp
    ):
        await _login_flow(env.client)
        (form,) = idp.token_forms
        assert "client_secret" not in form  # PKCE carried the proof
        break


async def test_explicit_endpoints_skip_discovery(tmp_path, monkeypatch):
    idp = MockIdP()
    async for env in _sso_env(
        tmp_path,
        monkeypatch,
        auth=_sso_auth(
            authorization_endpoint=f"{ISSUER}/authorize",
            token_endpoint=f"{ISSUER}/token",
            jwks_url=f"{ISSUER}/jwks",
        ),
        idp=idp,
    ):
        await _login_flow(env.client)
        assert "/.well-known/openid-configuration" not in idp.paths
        assert "/token" in idp.paths
        assert "/jwks" in idp.paths
        break


async def test_logout_drops_the_session_cookie(tmp_path, monkeypatch):
    async for env in _sso_env(tmp_path, monkeypatch):
        client = env.client
        await _login_flow(client)
        out = await client.post("/admin/ui/sso/logout")
        assert out.status_code == 200
        assert out.json() == {"ok": True}
        # The cookie is gone: the exchange honestly 401s.
        resp = await client.post("/admin/ui/sso/session")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "no_session"
        break


# ----------------------------------------------------------------------
# The principal: an identified platform admin, additive to raw keys
# ----------------------------------------------------------------------


async def test_sso_session_is_a_platform_admin(tmp_path, monkeypatch):
    async for env in _sso_env(tmp_path, monkeypatch):
        body = await _login_flow(env.client)
        hdrs = {"Authorization": f"Bearer {body['token']}"}
        # /admin/tenants is the platform-admin gate (tenant admins get
        # 403) — the SSO principal is the operator, no tenant.
        resp = await env.client.get("/admin/tenants", headers=hdrs)
        assert resp.status_code == 200, resp.text
        # And it can act, like the env admin key it parallels.
        resp = await env.client.post(
            "/admin/keys", json={"name": "by-sso"}, headers=hdrs
        )
        assert resp.status_code == 201, resp.text
        break


async def test_whoami_names_the_sso_human(tmp_path, monkeypatch):
    """Tier 61: the dashboard's identity probe names the signed-in
    human — the email and auth_method sso — where the raw admin key
    could only ever say "admin"."""
    async for env in _sso_env(tmp_path, monkeypatch):
        body = await _login_flow(env.client)
        resp = await env.client.get(
            "/admin/whoami", headers={"Authorization": f"Bearer {body['token']}"}
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "name": "alice@example.com",
            "auth_method": "sso",
            "is_admin": True,
            "platform_admin": True,
            "tenant": None,
        }


async def test_raw_admin_key_still_works_alongside_sso(tmp_path, monkeypatch):
    async for env in _sso_env(tmp_path, monkeypatch):
        resp = await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200
        break


async def test_audit_records_the_human(tmp_path, monkeypatch):
    """The point of the tier: the shared raw admin key could not tell
    humans apart; the SSO session's audit records name the email and
    auth_method 'sso'."""
    sink = AuditSink()
    config = make_config(
        auth=_sso_auth(),
        audit=AuditConfig(webhook_url="https://audit.test/hook"),
    )
    async for env in _sso_env(
        tmp_path,
        monkeypatch,
        config=config,
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        body = await _login_flow(env.client)
        created = await env.client.post(
            "/admin/keys",
            json={"name": "by-sso"},
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert created.status_code == 201, created.text

        async def _arrived() -> bool:
            return any(p["event"] == "key.created" for p in sink.payloads)

        await _wait_for(_arrived, what="the key.created audit record")
        rec = next(p for p in sink.payloads if p["event"] == "key.created")
        assert rec["actor"] == {"name": "alice@example.com", "auth_method": "sso"}
        break


# ----------------------------------------------------------------------
# Fail-fast at startup: a broken SSO setup never limps up
# ----------------------------------------------------------------------


async def test_fail_fast_when_session_secret_missing(tmp_path, monkeypatch):
    monkeypatch.delenv(SESSION_SECRET_ENV, raising=False)
    config = make_config(auth=_sso_auth())
    with pytest.raises(ValueError, match=SESSION_SECRET_ENV):
        async for _ in _make_env(tmp_path, config=config):
            break


async def test_fail_fast_on_incomplete_sso_config(tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_SECRET_ENV, SESSION_SECRET)
    config = make_config(auth=AuthConfig(sso=SSOConfig(enabled=True, client_id="x")))
    with pytest.raises(ValueError, match="issuer"):
        async for _ in _make_env(tmp_path, config=config):
            break


async def test_fail_fast_when_client_secret_env_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_SECRET_ENV, SESSION_SECRET)
    monkeypatch.delenv(CLIENT_SECRET_ENV, raising=False)
    config = make_config(auth=_sso_auth())
    with pytest.raises(ValueError, match=CLIENT_SECRET_ENV):
        async for _ in _make_env(tmp_path, config=config):
            break
