"""First-boot bootstrap: admin key and default virtual key."""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select

from .db import get_sessionmaker
from .models import SettingKV, VirtualKey
from .api.deps import hash_key

logger = logging.getLogger("gateway.bootstrap")


async def ensure_admin_key(settings) -> str:
    """Return the admin key.

    Precedence: GATEWAY_ADMIN_KEY env var > persisted generated key >
    newly generated (persisted + logged once).
    """
    if settings.admin_key:
        return settings.admin_key

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(SettingKV, "admin_key")
        if row is not None:
            return row.value

    raw = "sk-gw-admin-" + secrets.token_urlsafe(24)
    async with sessionmaker() as session:
        session.add(SettingKV(key="admin_key", value=raw))
        await session.commit()
    logger.warning(
        "No GATEWAY_ADMIN_KEY configured. Generated admin key (persisted): %s", raw
    )
    return raw


async def ensure_default_key() -> str | None:
    """Create a starter virtual key on an empty database. Returns raw key once."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        count = await session.scalar(select(func.count()).select_from(VirtualKey))
    if count:
        return None

    raw = "sk-gw-" + secrets.token_urlsafe(24)
    async with sessionmaker() as session:
        session.add(
            VirtualKey(name="default", key_hash=hash_key(raw), key_prefix=raw[:12])
        )
        await session.commit()
    logger.warning("Created default virtual key: %s", raw)
    return raw
