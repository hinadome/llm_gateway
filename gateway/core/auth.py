"""Pluggable authentication chain.

Authentication is a chain of ``Authenticator`` steps (config: ``auth.chain``).
Each step receives the raw credential exactly as the client sent it — the
transport-specific extraction (``Authorization: Bearer`` / ``x-api-key`` /
``x-goog-api-key`` / ``api-key`` / ``?key=``) stays in ``api/deps.py`` — and
either:

- returns an ``AuthContext`` (authenticated — the chain stops),
- returns ``None`` (not my credential — the next step tries), or
- raises ``AuthError`` (positively invalid — surfaces immediately).

Built-in steps:

- ``admin_key`` — the gateway's admin key (constant-time compare against the
  value seeded on ``app.state`` at startup).
- ``virtual_key`` — the raw credential is a gateway-issued virtual key
  (SHA-256 lookup); the key row is the single source of policy (allowlists,
  budgets, guardrail/model-routing overrides). Rotation: for the duration
  of a key's grace window the row's *previous* credential resolves to the
  same row — after the window it is refused with an explicit "rotated"
  error (see ``POST /admin/keys/{id}/rotate``).
- ``jwt`` — the credential is a JWT/OAuth bearer token (``header.payload.
  signature``). The token proves *identity* only: its ``key_claim``
  (default ``sub``) names the virtual key whose *policy* applies — so
  budgets, limits and logs stay in one place. Verification supports a
  shared HS256 ``secret`` or asymmetric keys fetched from a ``jwks_url``
  (cached, refreshed on unknown ``kid``); ``issuer``/``audience`` are
  enforced when configured, ``exp`` always is. Requires the ``auth`` extra
  (``pip install 'llm-gateway[auth]'``).

Chain entries may also be ``"pkg.module:ClassName"`` paths to custom
``Authenticator`` subclasses (instantiated with the ``AuthConfig``) — the
same dynamic-loading story guardrails have. Unknown names fail at startup,
not at first request.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import importlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value

from ..config import AuthConfig, JWTAuthConfig
from ..db import get_sessionmaker
from ..errors import AuthError, GatewayError
from ..models import Tenant, VirtualKey
from ..observability import metrics
from .context import AuthContext
from .logging_svc import get_key_by_hash, get_tenant_by_id

logger = logging.getLogger("gateway.auth")

# Signature algorithms we are willing to verify. Anything else (including
# "none", which is never a valid choice for authentication) is rejected
# before PyJWT ever sees the token.
_ALLOWED_ALGORITHMS = frozenset(
    {
        "HS256", "HS384", "HS512",
        "RS256", "RS384", "RS512",
        "ES256", "ES384", "ES512",
        "PS256", "PS384", "PS512",
    }
)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


def auth_context_from_key(
    vkey: VirtualKey,
    tenant: Tenant | None = None,
    auth_method: str | None = None,
) -> AuthContext:
    """Snapshot a virtual key row (and its tenant) into an AuthContext.

    ``tenant`` is the key's tenant row when it has one (``None`` for
    platform keys). Its policy ceiling is snapshotted alongside the
    key's — the same auth-time-consistency trade-off the key's budget
    fields make. ``auth_method`` records *how* the caller proved
    possession of this key ("virtual_key" for the raw key, "jwt" for a
    token naming it) — it lands on request-log rows and audit events.
    """
    return AuthContext(
        is_admin=bool(vkey.is_admin),
        key_id=vkey.id,
        key_name=vkey.name,
        tenant_id=(tenant.id if tenant is not None else None),
        tenant_name=(tenant.name if tenant is not None else None),
        tenant_allowed_models=(tenant.allowed_models if tenant is not None else None),
        tenant_allowed_tools=(tenant.allowed_tools if tenant is not None else None),
        tenant_allowed_mcp_servers=(
            tenant.allowed_mcp_servers if tenant is not None else None
        ),
        tenant_rpm_limit=(tenant.rpm_limit if tenant is not None else None),
        tenant_tpm_limit=(tenant.tpm_limit if tenant is not None else None),
        tenant_max_budget_usd=(
            tenant.max_budget_usd if tenant is not None else None
        ),
        tenant_spend_usd=(tenant.spend_usd if tenant is not None else 0.0),
        tenant_daily_budget_usd=(
            tenant.daily_budget_usd if tenant is not None else None
        ),
        tenant_monthly_budget_usd=(
            tenant.monthly_budget_usd if tenant is not None else None
        ),
        tenant_daily_spend_usd=(
            tenant.daily_spend_usd if tenant is not None else None
        ),
        tenant_daily_bucket=(tenant.daily_bucket if tenant is not None else None),
        tenant_monthly_spend_usd=(
            tenant.monthly_spend_usd if tenant is not None else None
        ),
        tenant_monthly_bucket=(
            tenant.monthly_bucket if tenant is not None else None
        ),
        tenant_pricing_markup_pct=(
            tenant.pricing_markup_pct if tenant is not None else None
        ),
        tenant_required_residencies=(
            tenant.required_residencies if tenant is not None else None
        ),
        tenant_max_concurrency=(
            tenant.max_concurrency if tenant is not None else None
        ),
        allowed_models=vkey.allowed_models,
        allowed_tools=vkey.allowed_tools,
        allowed_mcp_servers=vkey.allowed_mcp_servers,
        rpm_limit=vkey.rpm_limit,
        tpm_limit=vkey.tpm_limit,
        max_concurrency=vkey.max_concurrency,
        priority=vkey.priority or 0,
        required_residencies=vkey.required_residencies,
        max_budget_usd=vkey.max_budget_usd,
        spend_usd=vkey.spend_usd,
        daily_budget_usd=vkey.daily_budget_usd,
        monthly_budget_usd=vkey.monthly_budget_usd,
        daily_spend_usd=vkey.daily_spend_usd,
        daily_bucket=vkey.daily_bucket,
        monthly_spend_usd=vkey.monthly_spend_usd,
        monthly_bucket=vkey.monthly_bucket,
        disabled_guardrails=vkey.disabled_guardrails,
        cache_enabled=vkey.cache_enabled,
        cache_ttl_seconds=vkey.cache_ttl_seconds,
        model_mappings=vkey.model_mappings,
        default_model=vkey.default_model,
        system_prompt=vkey.system_prompt,
        system_prompt_ref=vkey.system_prompt_ref,
        prompt_variables=vkey.prompt_variables,
        allowed_endpoints=vkey.allowed_endpoints,
        allowed_ips=vkey.allowed_ips,
        auth_method=auth_method,
    )


# Tier 55: how often one key's last_used_at may be written while traffic
# keeps flowing. A write-amplification guard, deliberately not a config
# knob: it has no policy meaning (the timestamp is telemetry — "when was
# this key last seen", not "when did its budget change"), and 60s is
# finer than any decision (rotation, revocation, the idle filter) ever
# needs. Purely in-memory against the already-loaded row, so a burst of
# concurrent first-uses may all write — harmless, they stamp ~now.
_LAST_USED_INTERVAL_SECONDS = 60.0


async def record_key_use(vkey: VirtualKey) -> None:
    """Stamp last_used_at on a key traffic is about to ride.

    "Use" = a valid credential named this key and its policy is about
    to serve the request — recorded at the four sites where that
    happens (the virtual_key and jwt chain steps, playground
    impersonation, the batch worker's per-wave auth). auth runs first
    in the pipeline, so cache hits, guardrail blocks and rate-limit
    refusals still count: the caller proved possession either way. The
    replay harness deliberately never calls this — a replay must not
    mark a key alive.

    Throttled to one write per interval per key (checked against the
    loaded row — no extra read), best-effort (a failure here must
    never fail the request it observes). The Core UPDATE touches only
    last_used_at, so updated_at stays what it means: an edit marker.
    """
    now = datetime.now(timezone.utc)
    last = vkey.last_used_at
    if last is not None:
        if last.tzinfo is None:
            # SQLite hands stored datetimes back naive; the codebase
            # normalizes the same way (_is_expired) before comparing.
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < _LAST_USED_INTERVAL_SECONDS:
            return
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await session.execute(
                update(VirtualKey)
                .where(VirtualKey.id == vkey.id)
                .values(last_used_at=now)
            )
            await session.commit()
        # Keep the in-memory row honest too: it throttles the next
        # request's check without a DB read, and anything else holding
        # this row (same request, same process) sees the fresh value.
        # set_committed_value (not a plain assignment): the batch
        # worker calls this with a row still attached to its wave
        # session, where a dirty attribute would re-write the column
        # — and bump updated_at — on that session's flush. Committed
        # state = "the DB already says this; nothing to flush".
        set_committed_value(vkey, "last_used_at", now)
    except Exception:
        logger.warning(
            "last_used_at update failed for key %s — continuing without it",
            vkey.id,
            exc_info=True,
        )


async def get_key_by_name(name: str) -> VirtualKey | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(VirtualKey).where(VirtualKey.name == name)
        )
        return result.scalar_one_or_none()


async def load_tenant_for_key(vkey: VirtualKey) -> Tenant | None:
    """The key's tenant row, or None for platform keys.

    Fails closed on both failure modes: a dangling tenant_id (the
    delete endpoint refuses while keys reference a tenant, so a missing
    row is corrupted state — serving the key as an unrestricted
    platform key would bypass the tenant's policy) and a disabled
    tenant (the platform admin's kill switch for every member key).
    """
    if not vkey.tenant_id:
        return None
    tenant = await get_tenant_by_id(vkey.tenant_id)
    if tenant is None:
        raise AuthError(
            "This key's tenant no longer exists — ask the platform "
            "administrator to repair it."
        )
    if not tenant.is_active:
        raise AuthError("This key's tenant has been disabled.")
    return tenant


class Authenticator(abc.ABC):
    """One step in the authentication chain."""

    #: Chain-entry name (also the ``gateway_auth_total{authenticator}`` label
    #: and the ``auth_method`` recorded on request logs).
    name: str = "authenticator"

    @abc.abstractmethod
    async def authenticate(self, credential: str) -> AuthContext | None:
        """Return the authenticated principal, ``None`` to let the next
        step try, or raise ``AuthError`` for a positively-invalid
        credential."""


class AdminKeyAuthenticator(Authenticator):
    """The gateway's own admin key (constant-time compare)."""

    name = "admin_key"

    def __init__(self, admin_key_provider: Callable[[], str | None]) -> None:
        # Provider (not value): the key is seeded into app.state by the
        # lifespan, after the chain is built.
        self._provider = admin_key_provider

    async def authenticate(self, credential: str) -> AuthContext | None:
        admin_key = self._provider()
        if admin_key and hmac.compare_digest(credential, admin_key):
            return AuthContext(is_admin=True, key_name="admin", auth_method=self.name)
        return None  # maybe a virtual key / JWT — let the chain continue


class VirtualKeyAuthenticator(Authenticator):
    """The raw credential is a gateway-issued virtual key."""

    name = "virtual_key"

    async def authenticate(self, credential: str) -> AuthContext | None:
        vkey = await get_key_by_hash(hash_key(credential))
        if vkey is None:
            return None  # unknown here — maybe another step claims it
        if not vkey.is_active:
            raise AuthError()
        if _is_expired(vkey.expires_at):
            raise AuthError("This API key has expired.")
        if hash_key(credential) != vkey.key_hash:
            # Rotation: the credential matched the row's PREVIOUS hash.
            # The row's own checks above fired first — a revoked or
            # expired key refuses both credentials identically; only
            # then does the grace window decide. Inside it, the caller
            # is this key, full stop: same policy, same budgets, same
            # logs as the current credential. Past it (or with no
            # window at all — an immediate-cutover rotation), the old
            # credential is refused with an honest, actionable error
            # instead of a generic unknown-key 401.
            if vkey.grace_until is None or _is_expired(vkey.grace_until):
                raise AuthError(
                    "This API key has been rotated — its grace window "
                    "has ended. Use the key's current credential."
                )
        tenant = await load_tenant_for_key(vkey)
        # Last-used telemetry (tier 55): this credential — the row's
        # current one OR the previous one inside its grace window, both
        # reach here — just proved possession and the request is about
        # to ride this row's policy. Throttled + best-effort.
        await record_key_use(vkey)
        return auth_context_from_key(
            vkey, tenant=tenant, auth_method=self.name
        )


class JWTAuthenticator(Authenticator):
    """The credential is a JWT/OAuth bearer token naming a virtual key.

    The token proves identity; the named key row supplies policy (the same
    budgets, limits and overrides its raw key would get — spend, rate
    limits and logs accrue to it identically). Requires PyJWT (the
    ``auth`` extra).
    """

    name = "jwt"

    def __init__(
        self,
        config: JWTAuthConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        try:
            import jwt  # PyJWT — optional dependency (the `auth` extra)
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ValueError(
                "auth.chain includes 'jwt' but PyJWT is not installed. "
                "Install it with: pip install 'llm-gateway[auth]'"
            ) from exc
        if not config.secret and not config.jwks_url:
            raise ValueError(
                "auth.jwt needs a shared 'secret' (HS256) or a 'jwks_url' "
                "(asymmetric keys) to verify tokens against."
            )
        self._jwt = jwt
        self._config = config
        self._secret = config.secret
        self._algorithms = list(config.algorithms) if config.algorithms else (
            ["HS256"] if config.secret else ["RS256"]
        )
        disallowed = set(self._algorithms) - _ALLOWED_ALGORITHMS
        if disallowed:
            raise ValueError(
                f"auth.jwt.algorithms includes unsupported value(s): "
                f"{sorted(disallowed)}"
            )
        # JWKS cache (asymmetric verification only)
        self._jwks_url = config.jwks_url
        self._jwks_client = http_client
        self._owns_client = http_client is None
        self._keys: dict[str | None, dict] | None = None
        self._fetched_at = 0.0

    async def aclose(self) -> None:
        if self._owns_client and self._jwks_client is not None:
            await self._jwks_client.aclose()

    async def authenticate(self, credential: str) -> AuthContext | None:
        if credential.count(".") != 2:  # not a JWS compact token
            return None
        claims = await self._verify(credential)
        key_name = claims.get(self._config.key_claim)
        if not key_name or not isinstance(key_name, str):
            raise AuthError(
                f"Token carries no '{self._config.key_claim}' claim naming "
                f"a virtual key."
            )
        vkey = await get_key_by_name(key_name)
        if vkey is None:
            raise AuthError(
                f"Token subject '{key_name}' does not match a virtual key."
            )
        if not vkey.is_active:
            raise AuthError()
        if _is_expired(vkey.expires_at):
            raise AuthError("This API key has expired.")
        tenant = await load_tenant_for_key(vkey)
        # Last-used telemetry rides the token path too: the caller
        # proved possession of a credential naming this key, and its
        # policy is about to serve the request (tier 55).
        await record_key_use(vkey)
        return auth_context_from_key(vkey, tenant=tenant, auth_method=self.name)

    # -- verification ---------------------------------------------------------

    async def _verify(self, token: str) -> dict:
        """Verify signature + standard claims; return the payload."""
        try:
            if self._secret is not None:
                key = self._secret
            else:
                jwk = await self._jwk_for(token)
                key = self._jwt.PyJWK.from_dict(jwk).key
            return self._jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                issuer=self._config.issuer,
                audience=self._config.audience,
                leeway=self._config.leeway_seconds,
                options={"require": ["exp"]},
            )
        except self._jwt.InvalidTokenError as exc:
            raise AuthError(f"Invalid JWT: {exc}") from exc

    async def _jwk_for(self, token: str) -> dict:
        """The JWK for this token's ``kid``, from a cached JWKS."""
        try:
            header = self._jwt.get_unverified_header(token)
        except self._jwt.InvalidTokenError as exc:
            raise AuthError(f"Invalid JWT: {exc}") from exc
        kid = header.get("kid")
        if self._keys is None or time.monotonic() - self._fetched_at > (
            self._config.jwks_cache_seconds
        ):
            await self._refresh_jwks()
        if self._keys and kid in self._keys:
            return self._keys[kid]
        # Unknown kid: keys may have rotated since we cached — refresh once.
        await self._refresh_jwks()
        if self._keys and kid in self._keys:
            return self._keys[kid]
        if self._keys and kid is None and len(self._keys) == 1:
            # Single-key JWKS without kids: that key is the key.
            return next(iter(self._keys.values()))
        raise AuthError(f"JWT key id '{kid}' not found in the JWKS.")

    async def _refresh_jwks(self) -> None:
        if self._jwks_client is None:
            self._jwks_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        client = self._jwks_client
        try:
            response = await client.get(self._jwks_url)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # Fail closed with an honest status: the identity provider being
            # unreachable is not the client's fault, but tokens cannot be
            # verified either way.
            raise GatewayError(
                503,
                f"Could not fetch JWKS from the configured identity "
                f"provider: {exc}",
                "api_error",
                code="jwks_unavailable",
            ) from exc
        keys = {k.get("kid"): k for k in data.get("keys", [])}
        if not keys:
            raise AuthError("The configured JWKS contains no keys.")
        self._keys = keys
        self._fetched_at = time.monotonic()


def _load_class(class_path: str) -> type[Authenticator]:
    """Load a custom authenticator from a 'pkg.module:ClassName' path."""
    module_path, _, class_name = class_path.partition(":")
    if not module_path or not class_name:
        raise ValueError(
            f"Invalid authenticator class path '{class_path}' "
            f"(expected 'pkg.module:ClassName')"
        )
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, Authenticator)):
        raise TypeError(f"{class_path} is not an Authenticator subclass")
    return cls


class AuthChain:
    """The ordered authentication steps (``auth.chain``)."""

    def __init__(self, steps: list[Authenticator]) -> None:
        self._steps = steps

    @property
    def steps(self) -> list[Authenticator]:
        return list(self._steps)

    async def authenticate(self, credential: str) -> AuthContext:
        for step in self._steps:
            try:
                ctx = await step.authenticate(credential)
            except AuthError:
                metrics.AUTH.labels(authenticator=step.name, result="rejected").inc()
                raise
            if ctx is not None:
                metrics.AUTH.labels(
                    authenticator=step.name, result="authenticated"
                ).inc()
                return ctx
        raise AuthError()

    @classmethod
    def from_config(
        cls,
        config: AuthConfig,
        admin_key_provider: Callable[[], str | None],
        jwks_http_client: httpx.AsyncClient | None = None,
    ) -> "AuthChain":
        """Build the configured chain. Unknown entries fail here — at
        startup, not at the first request."""
        steps: list[Authenticator] = []
        for spec in config.chain:
            if spec == "admin_key":
                steps.append(AdminKeyAuthenticator(admin_key_provider))
            elif spec == "virtual_key":
                steps.append(VirtualKeyAuthenticator())
            elif spec == "jwt":
                steps.append(JWTAuthenticator(config.jwt, jwks_http_client))
            elif ":" in spec:
                steps.append(_load_class(spec)(config))
            else:
                raise ValueError(
                    f"Unknown authenticator '{spec}'. Use a built-in name "
                    f"(admin_key, virtual_key, jwt) or a "
                    f"'pkg.module:ClassName' path to your own Authenticator."
                )
        if not steps:
            raise ValueError("auth.chain must list at least one authenticator.")
        return cls(steps)
