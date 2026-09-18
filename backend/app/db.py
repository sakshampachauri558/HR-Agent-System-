"""Async SQLAlchemy engine, session factory, and thin SQL-text helpers.

No ORM models. Wave-1 agents write raw SQL (via `sqlalchemy.text`) with
bound parameters against the tables in `db/init.sql`. This module gives
them a session, a FastAPI dependency, and three convenience helpers
(`fetch_all`, `fetch_one`, `execute`) instead of everyone hand-rolling
`session.execute(text(...))` slightly differently.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


def _register_pgvector(dbapi_connection: Any, connection_record: Any) -> None:
    """Register the pgvector codec on every new asyncpg connection.

    Without this, `vector` columns round-trip as opaque strings instead of
    python lists/arrays. `dbapi_connection.run_async` is the hook
    SQLAlchemy's asyncpg dialect exposes for exactly this purpose (running
    an async setup coroutine against the raw asyncpg connection from a sync
    "connect" event).
    """
    try:
        from pgvector.asyncpg import register_vector

        dbapi_connection.run_async(lambda conn: register_vector(conn))
    except Exception:  # noqa: BLE001, S110 - deliberately swallowed, see below
        # Best-effort: if the pgvector python package changes its async
        # hook shape, don't take the whole app down over it — raw
        # `vector` columns still work as text, they just don't decode to
        # python lists automatically. Callers doing similarity search can
        # always cast explicitly.
        pass


def _register_json_codecs(dbapi_connection: Any, connection_record: Any) -> None:
    """Register `json`/`jsonb` codecs on every new asyncpg connection.

    Every table in `db/init.sql` that carries a `JSONB` column (`jobs.
    must_haves`/`rubric_weights`, `resumes.sections`, `evaluations.
    criteria`/`matched_skills`/`gaps`, `interview_kits.questions`,
    `audit_log.tool_trace`/`payload`, ...) is written via raw SQL through
    `db.execute()` with a plain Python `dict`/`list` bind param. Without
    this codec, asyncpg's default `jsonb` codec only accepts pre-
    serialized JSON text, so any such INSERT/UPDATE raises
    `asyncpg.exceptions.DataError: invalid input ... ('dict' object has
    no attribute 'encode')` — a failure every feature agent would
    otherwise hit independently the first time it persists structured
    data. With the codec registered, `dict`/`list` params round-trip
    transparently on write, and `SELECT`s decode straight back to
    `dict`/`list` instead of a raw JSON string.
    """

    async def _init(conn: Any) -> None:
        for typename in ("json", "jsonb"):
            await conn.set_type_codec(
                typename,
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
                format="text",
            )

    try:
        dbapi_connection.run_async(_init)
    except Exception:  # noqa: BLE001, S110 - deliberately swallowed, see below
        # Best-effort, mirroring `_register_pgvector` above: if this ever
        # can't attach, callers can still pass pre-serialized JSON text
        # and cast explicitly (`:col::jsonb`) as a fallback.
        pass


event.listens_for(engine.sync_engine, "connect")(_register_pgvector)
event.listens_for(engine.sync_engine, "connect")(_register_json_codecs)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager for a session outside of FastAPI's DI (scripts, the
    LLM layer's audit-log writes, background tasks). Commits on a clean
    exit, rolls back on exception.
    """
    session = async_session_maker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: `session: AsyncSession = Depends(get_session)`."""
    async with session_scope() as session:
        yield session


async def health() -> bool:
    """Lightweight liveness check: can we open a connection and round-trip
    `SELECT 1`? Used by `app.main._db_health()` for `/api/health`'s `db`
    field. Returns `False` (never raises) on any failure so a DB hiccup
    shows up as `db: "error: ..."` rather than crashing the health route.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - liveness check must never raise
        return False


def _rows_to_dicts(result: CursorResult) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


async def fetch_all(
    session: AsyncSession,
    query: str,
    params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run a SELECT and return every row as a plain dict."""
    result = await session.execute(text(query), params or {})
    return _rows_to_dicts(result)


async def fetch_one(
    session: AsyncSession,
    query: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run a SELECT and return the first row as a dict, or None."""
    result = await session.execute(text(query), params or {})
    row = result.first()
    return dict(row._mapping) if row is not None else None


async def execute(
    session: AsyncSession,
    query: str,
    params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> CursorResult:
    """Run an INSERT/UPDATE/DELETE (or DDL). Does not commit — the caller's
    session (request-scoped via `get_session`, or `session_scope`) commits
    on clean exit. Pass a list of dicts in `params` for `executemany`-style
    bulk writes.
    """
    return await session.execute(text(query), params or {})
