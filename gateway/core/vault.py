"""BYOK vault: tenant-owned provider credentials, encrypted at rest.

Bring-your-own-key in ten minutes instead of a new deployment: a
tenant admin stores their *provider* credentials once (``PUT
/admin/tenants/{id}/provider-keys/{provider}``) and every request from
that tenant rides their account instead of the platform's — chat,
embeddings, images, audio, all four inbound formats, retries and
fallbacks included. The provider still sees the same traffic; the
*bill* lands on the tenant's provider account, and the platform's
quota is untouched.

The security posture, in one breath: the raw key exists exactly
twice — in the caller's ``PUT`` body and in the upstream
``Authorization`` header. At rest it is Fernet-encrypted (AES128-CBC
+ HMAC-SHA256, authenticated) under a master key the gateway process
reads from ``GATEWAY_VAULT_MASTER_KEY`` and never persists; the admin
API returns only a *hint* (the last four characters, standard
practice for "which key is this?"); audit records carry the hint,
never the key; log rows record *that* BYOK was used (the provider
name) and nothing else.

Master-key rotation is MultiFernet-shaped: the setting accepts a
comma-separated list — the first key encrypts new entries, all keys
decrypt. Rotate by prepending the new key, restarting, re-PUTting
entries at leisure and dropping the old key once the fleet is
migrated. A key that falls out of the list makes its entries
undecryptable, and undecryptable is *fail-closed*: the request 500s
with ``byok_key_unreadable`` rather than silently falling back to the
platform key — a tenant that deliberately brought their own key must
never silently spend the platform's.

Lookups are cached per (tenant, provider) for ``ttl_seconds`` (30 by
default): the hot path adds one dict read, not a DB round trip.
Writes invalidate the local cache immediately; across a
multi-process fleet, ``vault.backend: redis`` (tier 34) additionally
publishes each invalidation on a Redis channel so every replica drops
its cached entry the moment a write lands — the TTL is the backstop
when a message is lost, and the whole mechanism when the backend is
left at ``memory``. Cache entries hold decrypted keys in memory for
their TTL — that is the point of a cache; process memory is the
trust boundary the master key already lives in.

Platform callers (no tenant) never touch the vault: their requests
use the platform's configured provider keys, exactly as before. The
vault is a tenant concept, and enabling it changes nothing for anyone
who hasn't stored a key.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, select

from ..db import get_sessionmaker
from ..errors import GatewayError
from ..models import ProviderKey

logger = logging.getLogger("gateway.vault")

# Provider ids come from config.yaml (providers:) — lowercase slugs,
# digits, dots, dashes, underscores.
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

# The decrypted key's last N characters, shown in the admin API so an
# operator can tell which stored key is which — the standard
# "••••1234" practice. Never enough to use.
HINT_CHARS = 4


class VaultDisabledError(GatewayError):
    """Raised by the admin API when the vault is not available — either
    no master key is configured or the encryption package is missing.
    Fail closed and loud: storing credentials unencrypted must never be
    a silent fallback."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            status_code=400,
            message=(
                "The BYOK vault is not available: "
                + reason
                + ". Credentials are only stored encrypted; refusing to "
                "store anything in plaintext."
            ),
            error_type="invalid_request_error",
            code="vault_unavailable",
        )


class VaultKeyUnreadableError(GatewayError):
    """An entry exists but cannot be decrypted (the master key was
    rotated without re-encrypting, or the row is corrupt). The request
    fails rather than silently billing the platform."""

    def __init__(self, tenant_id: str, provider: str) -> None:
        super().__init__(
            status_code=500,
            message=(
                f"The stored provider key for tenant '{tenant_id}' / "
                f"provider '{provider}' could not be decrypted — the "
                "vault master key may have been rotated without "
                "re-encrypting entries, or the entry is corrupt. "
                "Refusing to fall back to the platform key (fail "
                "closed); re-store the provider key to fix this."
            ),
            error_type="api_error",
            code="byok_key_unreadable",
        )


class ByokKeyRequiredError(GatewayError):
    """vault.strict is on and the caller's tenant has no stored key for
    ANY deployment of the requested model. The routing ladder for a
    strict tenant only contains providers the tenant keyed; an empty
    ladder means the request CANNOT run without spending the
    platform's credentials — refuse, and say exactly what to key."""

    def __init__(
        self,
        tenant_id: str,
        tenant_name: str | None,
        model: str,
        providers: list[str],
    ) -> None:
        who = f"tenant '{tenant_name}' ({tenant_id})" if tenant_name else (
            f"tenant '{tenant_id}'"
        )
        listed = ", ".join(sorted(providers)) or "none configured"
        super().__init__(
            status_code=403,
            message=(
                f"vault.strict is enabled: {who} has no stored provider "
                f"key for any deployment of model '{model}' (its "
                f"deployments run on: {listed}). The platform's own "
                "credentials may not serve tenant traffic while strict "
                "is on — store a provider key via PUT /admin/tenants/"
                f"{tenant_id}/provider-keys/<provider>, or set "
                "vault.strict: false."
            ),
            error_type="invalid_request_error",
            code="byok_key_required",
        )


@dataclass(frozen=True)
class VaultEntry:
    """A decrypted vault entry. ``key`` lives only in process memory —
    never in a log, a metric, a cache row or an API response."""

    tenant_id: str
    provider: str
    key: str
    label: str | None
    hint: str


def key_hint(key: str) -> str:
    """Last ``HINT_CHARS`` characters of a credential — enough to tell
    stored keys apart, never enough to use."""
    return "…" + key[-HINT_CHARS:] if len(key) > HINT_CHARS else "…" + key


class VaultService:
    """Stores and serves per-tenant provider credentials.

    ``master_key`` is a comma-separated list of Fernet keys
    (urlsafe-base64, 32 bytes — ``Fernet.generate_key()``); the first
    encrypts, all decrypt (MultiFernet rotation). ``None`` or an
    import-missing environment leaves the service *constructed but
    disabled*: admin calls get a 400 naming exactly what is missing,
    and the router skips lookups entirely.
    """

    def __init__(
        self,
        master_key: str | None = None,
        ttl_seconds: int = 30,
        on_invalidation: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        """``on_invalidation`` (tier 34) is fired with (tenant_id,
        provider) after every admin write's LOCAL cache drop — the
        Redis invalidator passes its ``publish`` here so a fleet of
        replicas converges immediately instead of within the TTL.
        Publishing is best-effort (the callback swallows its own
        transport errors; the TTL stays the backstop); None keeps the
        single-process behavior, byte for byte."""
        self.ttl_seconds = max(0, ttl_seconds)
        self._on_invalidation = on_invalidation
        self._fernet: Any | None = None
        self._missing_package: str | None = None
        self._invalid_key_reason: str | None = None
        if master_key is not None and master_key.strip():
            try:
                from cryptography.fernet import Fernet, MultiFernet
            except ImportError:  # pragma: no cover - environment-dependent
                self._missing_package = (
                    "install the encryption package "
                    "(pip install 'llm-gateway[vault]')"
                )
            else:
                keys = [k.strip() for k in master_key.split(",") if k.strip()]
                try:
                    # MultiFernet: the first key encrypts, every key
                    # decrypts — rotation without re-encrypting the world.
                    self._fernet = MultiFernet([Fernet(k) for k in keys])
                except Exception as exc:
                    self._invalid_key_reason = (
                        f"GATEWAY_VAULT_MASTER_KEY is not a valid Fernet "
                        f"key (expected base64 from Fernet.generate_key()): {exc}"
                    )
        # (tenant_id, provider) -> (entry, expires_at): decrypted keys
        # held in memory for the TTL. The DB row is only read on a miss.
        self._cache: dict[tuple[str, str], tuple[VaultEntry, datetime]] = {}
        # Negative cache: entries deleted elsewhere in the fleet must
        # not cost a DB round trip per request until the TTL passes.
        self._absent: dict[tuple[str, str], datetime] = {}
        self._lock = asyncio.Lock()

    # -- state ----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when credentials can actually be stored safely."""
        return self._fernet is not None

    @property
    def unavailable_reason(self) -> str | None:
        """Why the vault is disabled, for honest 400s. None = enabled."""
        if self._invalid_key_reason:
            return self._invalid_key_reason
        if self._missing_package:
            return self._missing_package
        return (
            "set GATEWAY_VAULT_MASTER_KEY (a Fernet key — "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\")"
        )

    def require_enabled(self) -> None:
        if not self.enabled:
            raise VaultDisabledError(self.unavailable_reason or "no master key")

    # -- invalidation -----------------------------------------------------------

    async def _notify_invalidation(self, tenant_id: str, provider: str) -> None:
        """Fan the local invalidation out (cross-replica) if a
        notifier is wired. Never raises: the local drop already
        happened, and a failed fan-out only means other replicas
        converge via the TTL — the documented backstop."""
        if self._on_invalidation is None:
            return
        try:
            await self._on_invalidation(tenant_id, provider)
        except Exception:  # noqa: BLE001
            logger.warning(
                "vault invalidation fan-out failed for (%s, %s) — other "
                "replicas converge within ttl_seconds",
                tenant_id,
                provider,
                exc_info=True,
            )

    # -- admin surface ----------------------------------------------------------

    @staticmethod
    def validate_provider(provider: str) -> None:
        if not _PROVIDER_RE.match(provider or ""):
            raise GatewayError(
                400,
                f"Invalid provider id '{provider}' — expected a lowercase "
                "slug matching the providers: section of config.yaml "
                "(e.g. 'openai', 'azure_eu').",
                "invalid_request_error",
            )

    async def put(
        self, tenant_id: str, provider: str, key: str, label: str | None = None
    ) -> dict:
        """Store (upsert) a tenant's credential. Returns metadata only —
        the key itself is never echoed back, not even to the caller
        that just sent it."""
        self.require_enabled()
        self.validate_provider(provider)
        if not key or not key.strip():
            raise GatewayError(
                400,
                "The provider key cannot be empty.",
                "invalid_request_error",
            )
        token = self._fernet.encrypt(key.strip().encode()).decode()
        hint = key_hint(key.strip())
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            existing = await session.execute(
                select(ProviderKey).where(
                    ProviderKey.tenant_id == tenant_id,
                    ProviderKey.provider == provider,
                )
            )
            row = existing.scalar_one_or_none()
            if row is None:
                row = ProviderKey(tenant_id=tenant_id, provider=provider)
                session.add(row)
            row.encrypted_key = token
            row.key_hint = hint
            row.label = label
            await session.commit()
            await session.refresh(row)
            result = row.to_public_dict()
        self.invalidate(tenant_id, provider)
        await self._notify_invalidation(tenant_id, provider)
        return result

    async def delete(self, tenant_id: str, provider: str) -> bool:
        """Remove a credential. True when a row was deleted."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                delete(ProviderKey).where(
                    ProviderKey.tenant_id == tenant_id,
                    ProviderKey.provider == provider,
                )
            )
            await session.commit()
            deleted = bool(result.rowcount)
        self.invalidate(tenant_id, provider)
        await self._notify_invalidation(tenant_id, provider)
        return deleted

    async def list_for_tenant(self, tenant_id: str) -> list[dict]:
        """Metadata for every credential a tenant stored — provider,
        label, hint, timestamps. Never the key."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(ProviderKey)
                .where(ProviderKey.tenant_id == tenant_id)
                .order_by(ProviderKey.provider)
            )
            return [row.to_public_dict() for row in result.scalars()]

    # -- hot path ---------------------------------------------------------------

    async def get(self, tenant_id: str, provider: str) -> VaultEntry | None:
        """The tenant's decrypted credential for a provider, or None.

        The router calls this once per dispatch attempt; the TTL cache
        keeps the hot path at a dict read. An entry that exists but
        cannot be decrypted raises VaultKeyUnreadableError — fail
        closed (the request must never silently spend the platform's
        key on a tenant that deliberately brought its own).
        """
        if not self.enabled or not tenant_id or not provider:
            return None
        now = datetime.now(timezone.utc)
        cached = self._cache.get((tenant_id, provider))
        if cached is not None and cached[1] > now:
            return cached[0]
        absent = self._absent.get((tenant_id, provider))
        if absent is not None and absent > now:
            return None

        async with self._lock:
            # Double-checked: another request may have warmed it while
            # we waited on the lock.
            now = datetime.now(timezone.utc)
            cached = self._cache.get((tenant_id, provider))
            if cached is not None and cached[1] > now:
                return cached[0]
            absent = self._absent.get((tenant_id, provider))
            if absent is not None and absent > now:
                return None

            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                result = await session.execute(
                    select(ProviderKey).where(
                        ProviderKey.tenant_id == tenant_id,
                        ProviderKey.provider == provider,
                    )
                )
                row = result.scalar_one_or_none()
            expiry = now + timedelta(seconds=self.ttl_seconds)
            if row is None:
                self._absent[(tenant_id, provider)] = expiry
                return None
            try:
                key = self._fernet.decrypt(row.encrypted_key.encode()).decode()
            except Exception as exc:
                # InvalidToken or a corrupted row: same honest outcome.
                raise VaultKeyUnreadableError(tenant_id, provider) from exc
            entry = VaultEntry(
                tenant_id=tenant_id,
                provider=provider,
                key=key,
                label=row.label,
                hint=row.key_hint,
            )
            self._cache[(tenant_id, provider)] = (entry, expiry)
            self._absent.pop((tenant_id, provider), None)
            return entry

    async def peek(self, tenant_id: str, provider: str) -> str | None:
        """Cache-only lookup: the provider name when a vault entry was
        recently served for this (tenant, provider), None otherwise.
        Used for observability (log row, response header, metric) —
        right after a dispatch the entry is always hot, so this adds a
        dict read and never a DB query. A cold cache answers None,
        which simply means "no header", never a wrong credential."""
        cached = self._cache.get((tenant_id, provider))
        if cached is None or cached[1] <= datetime.now(timezone.utc):
            return None
        return provider

    def invalidate(self, tenant_id: str, provider: str) -> None:
        """Drop the cached entry (and its negative twin) — called on
        every admin write. Same-process immediate; other replicas
        converge within ttl_seconds."""
        self._cache.pop((tenant_id, provider), None)
        self._absent.pop((tenant_id, provider), None)
