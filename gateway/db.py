"""Async SQLAlchemy engine/session management."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger("gateway.db")

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _add_missing_columns_sync(conn) -> None:
    """Upgrade existing tables in place: add model columns they lack.

    The project creates its schema with ``create_all`` (no Alembic), which
    never alters existing tables. This keeps existing databases upgradeable:
    every *nullable* column the models declare but the database lacks is
    added via ``ALTER TABLE ... ADD COLUMN`` (works on SQLite and Postgres).
    Missing single-column indexes are created too. Non-nullable additions
    cannot be auto-applied safely and are reported instead.
    """
    from sqlalchemy import inspect

    from .models import Base

    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all handles new tables
        existing_cols = {col["name"] for col in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            if not col.nullable:
                logger.warning(
                    "cannot auto-add NOT NULL column %s.%s — manual migration needed",
                    table.name,
                    col.name,
                )
                continue
            coltype = col.type.compile(conn.dialect)
            conn.execute(
                text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype} NULL')
            )
            logger.info("added column %s.%s", table.name, col.name)
            for index in table.indexes:
                if [c.name for c in index.columns] == [col.name]:
                    conn.execute(
                        text(
                            f'CREATE INDEX IF NOT EXISTS "{index.name}" '
                            f'ON "{table.name}" ("{col.name}")'
                        )
                    )


async def init_engine(database_url: str) -> AsyncEngine:
    """Create the engine (if needed) and create/upgrade tables."""
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(database_url, echo=False)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        from .models import Base  # local import to avoid circular dependency

        # Columns are added BEFORE create_all: create_all checks index
        # existence per table, and would try to (re)create an index whose
        # column is still missing on an upgraded database.
        await conn.run_sync(_add_missing_columns_sync)
        await conn.run_sync(Base.metadata.create_all)
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialised (call init_engine first)")
    return _sessionmaker


def get_engine() -> AsyncEngine:
    """The live engine (readiness checks, backups). Raises if not yet
    initialised — callers decide how to surface that."""
    if _engine is None:
        raise RuntimeError("Database engine not initialised (call init_engine first)")
    return _engine


def reset_engine_for_tests() -> None:
    """Allow tests to force re-initialisation with a fresh DB URL."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None
