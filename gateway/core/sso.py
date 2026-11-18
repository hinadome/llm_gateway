"""OIDC single sign-on for the admin dashboard (tier 60).

The dashboard's default login hands the browser the raw admin key — a
long-lived root credential living in sessionStorage. This module is
the replacement: an authorization-code + PKCE flow against the
operator's OIDC provider, producing a short-lived gateway-minted
session token. The browser holds that, never the admin key.

Shape (see api/sso.py for the routes):

* ``prepare_login`` builds the authorization redirect — random ``state``
  (CSRF binding) and a PKCE S256 ``verifier`` travel in a short-lived
  HttpOnly cookie, the code_verifier never in the URL;
* ``complete_login`` exchanges the code at the token endpoint (the
  client secret, when configured, rides the POST body — never the
  URL), then verifies the ID token against the provider's JWKS
  (signature, ``iss``, ``aud``, ``exp``, plus ``email_verified`` when
  the provider asserts it) and applies the email allowlist —
  every failure is an honest :class:`SsoError` the callback redirects
  back to the dashboard with;
* ``mint`` / ``verify_session`` are the session tokens themselves:
  plain HS256 JWTs signed with the session secret, ``iss`` fixed to
  :data:`SESSION_ISSUER`, ``sub`` the caller's email, ``exp`` the
  configured TTL. Stateless — replicas share nothing but the secret;
  revocation is logout-locally plus the short TTL (documented trade).

Everything here degrades honestly: PyJWT missing, a misconfigured
provider or an unset secret all surface as startup errors naming the
fix; every request-time failure is a miss with a reason, never a
stack trace in the browser.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode

import httpx

from ..errors import AuthError

if TYPE_CHECKING:  # pragma: no cover
    from ..config import SSOConfig

logger = logging.getLogger("gateway.sso")

# The fixed ``iss`` claim of gateway-minted session tokens — how the
# chain step (SsoSessionAuthenticator) recognizes its own tokens.
SESSION_ISSUER = "llm-gateway:sso"
# The ``aud`` claim of session tokens: they open the admin surface.
SESSION_AUDIENCE = "admin"


class SsoError(Exception):
    """An honest, user-facing SSO failure. ``reason`` is a stable slug
    the dashboard can key on; ``message`` the human explanation."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class SsoService:
    """The OIDC login flow + session mint/verify, config-driven.

    Constructed in ``create_app`` only when ``auth.sso.enabled`` (and
    fail-fast validated there); the routes and the chain step both
    read from one instance on ``app.state``.
    """

    def __init__(
        self,
        config: "SSOConfig",
        session_secret: str,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            import jwt  # PyJWT — optional dependency (the `auth` extra)
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ValueError(
                "auth.sso is enabled but PyJWT is not installed. "
                "Install it with: pip install 'llm-gateway[auth]'"
            ) from exc
        self._jwt = jwt
        self.config = config
        self._session_secret = session_secret
        self._client = http_client
        self._owns_client = http_client is None
        self._clock = clock
        # IdP metadata (discovery), cached.
        self._metadata: dict[str, Any] | None = None
        self._metadata_fetched_at = 0.0
        # JWKS for ID-token verification, cached; unknown kid refetches.
        self._keys: dict[str | None, dict] | None = None
        self._keys_fetched_at = 0.0

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        """The HTTP client for identity-provider traffic: an injected one
        wins (tests), else a dedicated client this service owns and
        closes — non-instrumented either way, like the JWKS fetch and
        alert webhooks, so IdP round trips stay out of request traces."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    # -- login -----------------------------------------------------------

    async def prepare_login(self) -> tuple[str, str, str]:
        """Build the authorization redirect — ``(redirect_url, state,
        verifier)``; the caller puts state + verifier in the HttpOnly
        cookie, and only state rides the URL. PKCE S256 always: it
        costs a public client nothing and hardens a confidential one.
        Ensures discovery first (the redirect needs the authorization
        endpoint)."""
        await self._metadata_or_discover()
        assert self._metadata is not None
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": "openid email",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._metadata['authorization_endpoint']}?{query}", state, verifier

    async def complete_login(
        self, code: str, code_verifier: str, client_secret: str | None
    ) -> dict[str, str]:
        """Exchange the authorization code for tokens, verify the ID
        token, apply the email allowlist; returns the minted session
        (``token`` + ``email``). Raises SsoError honestly."""
        metadata = await self._metadata_or_discover()
        token_endpoint = metadata["token_endpoint"]
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": code_verifier,
        }
        if client_secret:
            # Confidential client: the secret rides the POST body
            # (never the URL — proxies and browser histories keep it).
            form["client_secret"] = client_secret
        client = self._ensure_client()
        try:
            resp = await client.post(token_endpoint, data=form)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("SSO token exchange failed: %s", exc)
            raise SsoError(
                "token_exchange_failed",
                "The identity provider rejected the sign-in exchange.",
            ) from exc
        id_token = payload.get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise SsoError(
                "no_id_token",
                "The identity provider returned no ID token.",
            )
        claims = await self._verify_id_token(id_token)
        email = claims.get("email")
        if not email or not isinstance(email, str):
            raise SsoError(
                "no_email",
                "The ID token carries no email — request the 'email' scope.",
            )
        email = email.strip().lower()
        # If the provider asserts email_verified: false, the address is
        # not the caller's — an allowlist match on it would be a hole.
        verified = claims.get("email_verified")
        if verified is False:
            raise SsoError(
                "email_not_verified",
                "The identity provider has not verified this email address.",
            )
        if not self._email_allowed(email):
            raise SsoError(
                "not_allowed",
                f"'{email}' is not on the SSO allowlist.",
            )
        return {"token": self.mint(email), "email": email}

    def _email_allowed(self, email: str) -> bool:
        cfg = self.config
        if email in {e.strip().lower() for e in cfg.allowed_emails}:
            return True
        domain = email.rpartition("@")[2]
        return any(
            domain == d.strip().lower().lstrip("@")
            for d in cfg.allowed_domains
        )

    # -- sessions ---------------------------------------------------------

    def is_session_token(self, token: str) -> bool:
        """Cheap, *unverified* peek: does this credential claim to be a
        gateway session (``iss`` fixed to :data:`SESSION_ISSUER`)?

        The chain step (``SsoSessionAuthenticator``) uses it to decide
        whether to claim the credential — claiming authenticates
        nothing: a claimed token still goes through full verification,
        and a foreign JWT falls through to the ``jwt`` step untouched.
        """
        try:
            claims = self._jwt.decode(token, options={"verify_signature": False})
        except self._jwt.InvalidTokenError:
            return False
        return claims.get("iss") == SESSION_ISSUER

    def mint(self, email: str) -> str:
        """Mint a session token for an allowlisted email: HS256, iss
        fixed, exp = the configured TTL."""
        now = int(self._clock())
        return self._jwt.encode(
            {
                "iss": SESSION_ISSUER,
                "aud": SESSION_AUDIENCE,
                "sub": email,
                "iat": now,
                "exp": now + int(self.config.session_ttl_seconds),
            },
            self._session_secret,
            algorithm="HS256",
        )

    def verify_session(self, token: str) -> str:
        """Verify a gateway-minted session token; returns the email.
        Raises AuthError (the chain step's rejection currency) on any
        failure."""
        try:
            claims = self._jwt.decode(
                token,
                self._session_secret,
                algorithms=["HS256"],
                issuer=SESSION_ISSUER,
                audience=SESSION_AUDIENCE,
                options={"require": ["exp", "sub"]},
            )
        except self._jwt.InvalidTokenError as exc:
            raise AuthError("Sign-in session expired or invalid — sign in again.") from exc
        email = claims.get("sub")
        if not email or not isinstance(email, str):
            raise AuthError("Sign-in session carries no subject.")
        return email

    # -- ID-token verification ----------------------------------------------

    async def _verify_id_token(self, token: str) -> dict:
        """Verify the IdP's ID token: signature via the JWKS, ``iss``,
        ``aud`` == client_id, ``exp`` required (leeway 30s)."""
        try:
            header = self._jwt.get_unverified_header(token)
        except self._jwt.InvalidTokenError as exc:
            raise SsoError("bad_id_token", "The ID token is malformed.") from exc
        kid = header.get("kid")
        jwk = await self._jwk_for(kid)
        try:
            return self._jwt.decode(
                token,
                self._jwt.PyJWK.from_dict(jwk).key,
                algorithms=["RS256", "ES256"],
                issuer=self.config.issuer,
                audience=self.config.client_id,
                leeway=30.0,
                options={"require": ["exp"]},
            )
        except self._jwt.InvalidTokenError as exc:
            logger.warning("SSO ID token verification failed: %s", exc)
            raise SsoError("bad_id_token", "The ID token failed verification.") from exc

    async def _jwk_for(self, kid: str | None) -> dict:
        assert self._metadata is not None  # discovery ran before us
        if self._keys is None or self._clock() - self._keys_fetched_at > (
            self.config.discovery_cache_seconds
        ):
            await self._refresh_keys()
        if self._keys and kid in self._keys:
            return self._keys[kid]
        await self._refresh_keys()  # unknown kid: maybe rotated — refetch once
        if self._keys and kid in self._keys:
            return self._keys[kid]
        if self._keys and kid is None and len(self._keys) == 1:
            return next(iter(self._keys.values()))
        raise SsoError(
            "bad_id_token", f"The ID token's key id '{kid}' is not published by the provider."
        )

    async def _refresh_keys(self) -> None:
        assert self._metadata is not None
        client = self._ensure_client()
        try:
            resp = await client.get(self._metadata["jwks_url"])
            resp.raise_for_status()
            jwks = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("SSO JWKS fetch failed: %s", exc)
            raise SsoError("jwks_unavailable", "The provider's signing keys are unreachable.") from exc
        keys = {k.get("kid"): k for k in jwks.get("keys", []) if isinstance(k, dict)}
        self._keys = keys
        self._keys_fetched_at = self._clock()

    # -- discovery ---------------------------------------------------------

    async def _metadata_or_discover(self) -> dict[str, Any]:
        """The provider's endpoints — explicit overrides, or discovery
        (cached for discovery_cache_seconds)."""
        cfg = self.config
        explicit = {
            "authorization_endpoint": cfg.authorization_endpoint,
            "token_endpoint": cfg.token_endpoint,
            "jwks_url": cfg.jwks_url,
        }
        if all(explicit.values()):
            self._metadata = explicit  # type: ignore[assignment]
            return self._metadata
        if (
            self._metadata is None
            or self._clock() - self._metadata_fetched_at > cfg.discovery_cache_seconds
        ):
            client = self._ensure_client()
            url = f"{cfg.issuer.rstrip('/')}/.well-known/openid-configuration"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                doc = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("SSO discovery failed: %s", exc)
                raise SsoError(
                    "discovery_failed",
                    f"The identity provider's discovery document is unreachable ({url}).",
                ) from exc
            # Explicit overrides win field-by-field over discovery.
            merged = {
                "authorization_endpoint": doc.get("authorization_endpoint"),
                "token_endpoint": doc.get("token_endpoint"),
                "jwks_url": doc.get("jwks_uri"),
            }
            for field_name, value in explicit.items():
                if value:
                    merged[field_name] = value
            missing = [k for k, v in merged.items() if not v]
            if missing:
                raise SsoError(
                    "discovery_incomplete",
                    f"The discovery document lacks: {', '.join(sorted(missing))}.",
                )
            self._metadata = merged
            self._metadata_fetched_at = self._clock()
            # A metadata change may mean new signing keys.
            self._keys = None
        assert self._metadata is not None
        return self._metadata
