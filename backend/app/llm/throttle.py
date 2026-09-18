"""Shared throttling primitives for the LLM layer.

- A process-wide async token bucket capped at `LLM_RPM`.
- A daily request counter backed by `audit_log`, enforcing `LLM_DAILY_BUDGET`.
- A hand-rolled retry-with-backoff wrapper (no extra dependency) that treats
  429/502/503 identically, per PRD §10.
- The two typed exceptions the rest of the app (routers, `provider.py`)
  catches to build the `{"error": {"code", "message"}}` envelope.

`backend/app/llm/provider.py` is the only expected caller of this module;
Wave-1 agents should not need to import it directly.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import execute, fetch_one

T = TypeVar("T")

# Every successful LLM call (real or mock) writes one audit_log row with
# this action. The daily budget counter and F6's usage chart both count/
# group on it.
AUDIT_ACTION_LLM_CALL = "llm_call"

RETRIABLE_STATUS_CODES = frozenset({429, 502, 503})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for typed LLM-layer failures."""


class BudgetExhausted(LLMError):
    """Raised before an LLM call is attempted when today's UTC request
    count has already reached `LLM_DAILY_BUDGET`. Routers should map this
    to HTTP 429, code "llm_budget_exhausted"."""

    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            f"Daily LLM request budget exhausted: {used}/{limit} used today (UTC)."
        )


class LLMRateLimited(LLMError):
    """Raised after retries (and, if configured, a failover attempt) are
    exhausted. Routers should map this to HTTP 429, code
    "llm_rate_limited", surfacing `retry_after` so the UI can show a
    countdown instead of a generic error."""

    def __init__(self, retry_after: int, message: str = "LLM rate limit exceeded") -> None:
        self.retry_after = retry_after
        super().__init__(message)


# ---------------------------------------------------------------------------
# Token bucket (RPM limiter)
# ---------------------------------------------------------------------------


class TokenBucket:
    """A simple async token bucket. `capacity` tokens refill continuously
    at `capacity` per 60 seconds (i.e. `LLM_RPM` requests/minute)."""

    def __init__(self, capacity: int) -> None:
        self.capacity = float(max(capacity, 1))
        self._tokens = self.capacity
        self._rate_per_sec = self.capacity / 60.0
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._updated_at = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self._rate_per_sec)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait_s = (1 - self._tokens) / self._rate_per_sec
                await asyncio.sleep(wait_s)


_bucket: TokenBucket | None = None


def get_bucket() -> TokenBucket:
    global _bucket
    if _bucket is None:
        _bucket = TokenBucket(settings.llm_rpm)
    return _bucket


async def acquire() -> None:
    """Block until the shared bucket has a token for this call."""
    await get_bucket().acquire()


# ---------------------------------------------------------------------------
# Daily budget (backed by audit_log)
# ---------------------------------------------------------------------------


async def budget_used_today(session: AsyncSession) -> int:
    row = await fetch_one(
        session,
        """
        SELECT count(*) AS n
        FROM audit_log
        WHERE action = :action
          AND (created_at AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date
          -- Only calls that actually consume the hosted free-tier quota count
          -- against the daily budget. `mock` makes no network request at all,
          -- and `ollama` runs locally with no provider-side limit; billing
          -- either one would let the parallel build exhaust a counter whose
          -- whole purpose is protecting the ~50 real requests/day.
          AND coalesce(payload->>'provider', '') NOT IN ('mock', 'ollama')
        """,
        {"action": AUDIT_ACTION_LLM_CALL},
    )
    return int(row["n"]) if row else 0


async def check_budget(session: AsyncSession) -> int:
    """Returns requests used today. Raises `BudgetExhausted` if the daily
    cap has already been reached — call this *before* attempting a call."""
    used = await budget_used_today(session)
    if used >= settings.llm_daily_budget:
        raise BudgetExhausted(used=used, limit=settings.llm_daily_budget)
    return used


async def record_llm_call(
    session: AsyncSession,
    *,
    feature: str,
    provider: str,
    model: str,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Write the audit_log row every completed LLM call (real or mock)
    must produce. Feeds both the daily counter and F6's usage chart."""
    await execute(
        session,
        """
        INSERT INTO audit_log (entity_type, entity_id, action, actor, tool_trace, payload)
        VALUES (:entity_type, NULL, :action, :actor, NULL, :payload)
        """,
        {
            "entity_type": "llm_call",
            "action": AUDIT_ACTION_LLM_CALL,
            "actor": feature,
            "payload": {
                "provider": provider,
                "model": model,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "feature": feature,
            },
        },
    )


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------


def status_code_of(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an `openai` SDK
    exception (or anything else that quacks like one)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def is_retriable(exc: BaseException) -> bool:
    status = status_code_of(exc)
    return status in RETRIABLE_STATUS_CODES


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
) -> T:
    """Exponential backoff with jitter over `fn`. Retries only on
    429/502/503 (per `is_retriable`); anything else propagates
    immediately. Re-raises the last exception once `max_attempts` is
    exhausted."""
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_retriable(exc):
                raise
            delay = base_delay_s * (2 ** (attempt - 1)) + random.uniform(0, base_delay_s)
            await asyncio.sleep(delay)
    # Unreachable, but keeps type-checkers happy.
    assert last_exc is not None
    raise last_exc


def suggested_retry_after() -> int:
    """A conservative "come back in N seconds" hint for `LLMRateLimited`,
    derived from the configured RPM rather than a magic number."""
    return max(5, int(60 / max(settings.llm_rpm, 1)) * 3)
