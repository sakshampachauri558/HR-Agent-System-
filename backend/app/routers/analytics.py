"""F6 — HR Analytics. PRD §4 F6 / §9 contract.

    GET  /analytics        -> {pipeline, score_distribution, skill_gaps, usage}
    POST /analytics/query  -> {sql, rows, explanation}

Mounted under /api by app/main.py's router discovery (this module exposes
`router = APIRouter()` with routes registered WITHOUT the /api prefix).

`POST /analytics/query` is the one place in the whole app where model
output could reach the database, so it is treated as hostile input end to
end (PRD §4 F6):

  - The model's ONLY allowed output shape is `AnalyticsQueryPlan` -- a
    structured table/columns/filters/group_by/aggregation/order/limit
    description. It has no `sql` field. Even the `analytics_query` fixture
    in `seed/mock_responses.json` carries a canned `sql`/`rows`/
    `explanation` (kept there for whichever design a builder picked);
    Pydantic's default `extra="ignore"` silently drops those fields the
    moment the fixture is validated against `AnalyticsQueryPlan`, so they
    never reach this module. We build the SQL ourselves, always.
  - Every table name and every column name the plan mentions (`table`,
    `columns`, `group_by`, `order_by`, and each key of `filters`) is
    checked against the hardcoded `ALLOWLIST` below in `build_sql()`
    *before* any SQL text is composed. Anything unknown raises
    `AnalyticsQueryError`, mapped to HTTP 400 -- never silently dropped or
    passed through.
  - No value from the model is ever interpolated into SQL text: every
    filter value is a bound `:paramN`, and `LIMIT` is always a bound
    parameter too.
  - The composed SQL executes under the read-only `analytics_ro` role
    (`db/init.sql`) via `SET LOCAL ROLE` for the one transaction that runs
    it -- see `_run_readonly()` for exactly what that does and does not
    buy us.
  - `resumes.raw_text` and `resumes.redacted_text` are not in the
    allowlist at all. That box does not answer with candidate PII.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import execute, fetch_all, get_session
from app.llm.provider import get_agent
from app.llm.throttle import budget_used_today
from app.schemas import AnalyticsQueryRequest

router = APIRouter()

# ---------------------------------------------------------------------------
# Allowlist -- the only tables/columns the NL->SQL path may ever touch.
# logical_column_name -> actual SQL expression to select/filter/group/order
# on. `resumes.raw_text` / `resumes.redacted_text` are deliberately absent:
# those hold candidate PII (PRD §8) and this box only ever reads
# `candidate_label`, the generated pseudonym.
#
# `audit_log.payload` is JSONB; a few of its keys (provider/model/feature/
# latency_ms/input_tokens/output_tokens) are exposed as flat logical
# columns via `payload->>'x'` so usage questions ("requests by provider")
# are answerable without ever allowlisting the raw JSONB blob itself.
# ---------------------------------------------------------------------------
ALLOWLIST: dict[str, dict[str, str]] = {
    "resumes": {
        "id": "id",
        "candidate_label": "candidate_label",
        "file_name": "file_name",
        "status": "status",
        "uploaded_at": "uploaded_at",
    },
    "jobs": {
        "id": "id",
        "title": "title",
        "level": "level",
        "location": "location",
        "department": "department",
        "min_years": "min_years",
        "comp_min": "comp_min",
        "comp_max": "comp_max",
        "created_at": "created_at",
    },
    "evaluations": {
        "id": "id",
        "resume_id": "resume_id",
        "job_id": "job_id",
        "fit_score": "fit_score",
        "recommendation": "recommendation",
        "matched_skills": "matched_skills",
        "gaps": "gaps",
        "summary": "summary",
        "provider": "provider",
        "model": "model",
        "rubric_version": "rubric_version",
        "prompt_version": "prompt_version",
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "latency_ms": "latency_ms",
        "created_at": "created_at",
    },
    "interview_kits": {
        "id": "id",
        "evaluation_id": "evaluation_id",
        "created_at": "created_at",
    },
    "documents": {
        "id": "id",
        "title": "title",
        "kind": "kind",
        "source_name": "source_name",
        "char_count": "char_count",
        "uploaded_at": "uploaded_at",
    },
    "chunks": {
        "id": "id",
        "document_id": "document_id",
        "section": "section",
        "ordinal": "ordinal",
        "token_count": "token_count",
    },
    "audit_log": {
        "id": "id",
        "entity_type": "entity_type",
        "entity_id": "entity_id",
        "action": "action",
        "actor": "actor",
        "created_at": "created_at",
        "provider": "payload->>'provider'",
        "model": "payload->>'model'",
        "feature": "payload->>'feature'",
        "latency_ms": "(payload->>'latency_ms')::int",
        "input_tokens": "(payload->>'input_tokens')::int",
        "output_tokens": "(payload->>'output_tokens')::int",
    },
}


class AnalyticsQueryError(Exception):
    """Table/column outside `ALLOWLIST`, or an otherwise malformed plan.
    Always mapped to HTTP 400 in the route -- never passed through."""


class AnalyticsQueryPlan(BaseModel):
    """The model's ONLY allowed output shape for NL->SQL. A structured
    query description, never a SQL string (PRD §4 F6) -- there is no `sql`
    field on this model, on purpose."""

    table: str
    columns: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)  # logical_column -> equality value
    group_by: list[str] = Field(default_factory=list)
    aggregation: Literal["count", "sum", "avg", "min", "max", "none"] = "none"
    aggregation_column: str | None = None
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=50, ge=1, le=200)


def _allowlist_prompt_block() -> str:
    lines = [f"- {table}: {', '.join(sorted(cols))}" for table, cols in ALLOWLIST.items()]
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You translate a natural-language analytics question about an HR
recruiting pipeline into a STRUCTURED QUERY PLAN. You never write SQL --
you only ever choose a table, columns, filters, a group-by, an
aggregation, an order, and a limit, all restricted to the allowlist below.
Anything you name outside this list will be rejected before it reaches
the database.

Allowed tables and columns (nothing else exists as far as you're concerned):
{_allowlist_prompt_block()}

Rules:
- `table` must be exactly one of the table names above.
- `columns`, `group_by`, `order_by`, and every key of `filters` must be a
  column name allowed for the chosen table.
- Candidate resumes' raw or redacted text is never available to you and
  must never be requested, in this question or any other.
- Use `aggregation="count"` with a `group_by` for "how many" / "breakdown
  by" questions. Use `aggregation="none"` for a plain row listing.
- Keep `limit` small (<=50) unless the question clearly calls for more.
"""


# ---------------------------------------------------------------------------
# SQL composition -- the only function allowed to turn a plan into text.
# ---------------------------------------------------------------------------


def _resolve_table(table: str) -> dict[str, str]:
    if table not in ALLOWLIST:
        raise AnalyticsQueryError(f"Unknown table {table!r}. Allowed tables: {sorted(ALLOWLIST)}")
    return ALLOWLIST[table]


def _resolve_column(columns_map: dict[str, str], name: str, *, table: str) -> str:
    if name not in columns_map:
        raise AnalyticsQueryError(
            f"Unknown column {name!r} for table {table!r}. Allowed columns: {sorted(columns_map)}"
        )
    return columns_map[name]


def build_sql(plan: AnalyticsQueryPlan) -> tuple[str, dict[str, Any]]:
    """Compose a parameterized, read-only SELECT from a validated
    `AnalyticsQueryPlan`. Every table/column name is checked against
    `ALLOWLIST` here; nothing from `plan` is ever interpolated into the
    SQL text as a *value* -- values only ever go in via `params`, bound
    with `:name` placeholders the way `app.db.fetch_all` expects.
    """
    columns_map = _resolve_table(plan.table)

    select_parts: list[str] = []
    seen_aliases: set[str] = set()

    def add_select(alias: str, expr: str) -> None:
        if alias in seen_aliases:
            return
        seen_aliases.add(alias)
        select_parts.append(f"{expr} AS {alias}")

    # Group-by columns must appear in SELECT for a valid GROUP BY query;
    # add them first so they lead the result columns.
    for name in plan.group_by:
        add_select(name, _resolve_column(columns_map, name, table=plan.table))

    for name in plan.columns:
        add_select(name, _resolve_column(columns_map, name, table=plan.table))

    if plan.aggregation != "none":
        if plan.aggregation == "count" and not plan.aggregation_column:
            agg_expr = "count(*)"
        else:
            if not plan.aggregation_column:
                raise AnalyticsQueryError(f"aggregation={plan.aggregation!r} requires aggregation_column")
            col_expr = _resolve_column(columns_map, plan.aggregation_column, table=plan.table)
            agg_expr = f"{plan.aggregation}({col_expr})"
        add_select("agg_value", agg_expr)

    if not select_parts:
        # Nothing requested at all -- fall back to a row count rather than
        # guessing a column to select.
        add_select("agg_value", "count(*)")

    params: dict[str, Any] = {}
    where_parts: list[str] = []
    for i, (name, value) in enumerate(plan.filters.items()):
        expr = _resolve_column(columns_map, name, table=plan.table)
        param_name = f"filter_{i}"
        where_parts.append(f"{expr} = :{param_name}")
        params[param_name] = value

    group_parts = [_resolve_column(columns_map, name, table=plan.table) for name in plan.group_by]

    order_sql: str | None = None
    if plan.order_by:
        order_sql = plan.order_by if plan.order_by in seen_aliases else _resolve_column(
            columns_map, plan.order_by, table=plan.table
        )
    elif plan.aggregation != "none":
        order_sql = "agg_value"

    limit = max(1, min(plan.limit or 50, 200))
    params["limit"] = limit

    sql = f"SELECT {', '.join(select_parts)} FROM {plan.table}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)
    if order_sql:
        sql += f" ORDER BY {order_sql} {'ASC' if plan.order_dir == 'asc' else 'DESC'}"
    sql += " LIMIT :limit"

    return sql, params


def _explain(plan: AnalyticsQueryPlan) -> str:
    bits = [f"Reading from `{plan.table}`"]
    if plan.filters:
        bits.append("filtered on " + ", ".join(f"{k}={v!r}" for k, v in plan.filters.items()))
    if plan.group_by:
        bits.append("grouped by " + ", ".join(plan.group_by))
    if plan.aggregation != "none":
        agg_desc = (
            "count(*)"
            if plan.aggregation == "count" and not plan.aggregation_column
            else f"{plan.aggregation}({plan.aggregation_column})"
        )
        bits.append(f"aggregating {agg_desc}")
    limit = max(1, min(plan.limit or 50, 200))
    bits.append(f"limited to {limit} rows")
    return (
        ", ".join(bits)
        + ". Executed read-only as the analytics_ro Postgres role against an allowlisted "
        "table/column set -- the model never produced SQL text, and no free-form query "
        "reached the database."
    )


async def _run_readonly(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute `sql` under the read-only `analytics_ro` role (`db/init.sql`)
    for the lifetime of this transaction, via `SET LOCAL ROLE`. This is
    defense-in-depth *underneath* the allowlist, not a replacement for it:
    `analytics_ro` has table-level `SELECT` granted on every table in the
    schema (per `db/init.sql`'s `GRANT SELECT ON ALL TABLES`), including
    `resumes` -- so the role itself does not know to keep `raw_text` out
    of reach. What it does guarantee is that this code path can never
    INSERT/UPDATE/DELETE/DROP, no matter what `build_sql` ever produces.
    """
    await execute(session, "SET LOCAL ROLE analytics_ro")
    try:
        return await fetch_all(session, sql, params)
    finally:
        await execute(session, "RESET ROLE")


# ---------------------------------------------------------------------------
# GET /analytics
# ---------------------------------------------------------------------------

_STATUS_ORDER: tuple[str, ...] = ("queued", "parsing", "evaluating", "scored", "failed")
_SCORE_BUCKET_LABELS: tuple[str, ...] = ("0-20", "21-40", "41-60", "61-80", "81-100")


async def _pipeline(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await fetch_all(session, "SELECT status, count(*) AS count FROM resumes GROUP BY status")
    counts = {r["status"]: int(r["count"]) for r in rows}
    return [{"status": s, "count": counts.get(s, 0)} for s in _STATUS_ORDER]


async def _score_distribution(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await fetch_all(
        session,
        """
        SELECT
          CASE
            WHEN fit_score BETWEEN 0 AND 20 THEN '0-20'
            WHEN fit_score BETWEEN 21 AND 40 THEN '21-40'
            WHEN fit_score BETWEEN 41 AND 60 THEN '41-60'
            WHEN fit_score BETWEEN 61 AND 80 THEN '61-80'
            WHEN fit_score BETWEEN 81 AND 100 THEN '81-100'
          END AS range_label,
          count(*) AS count
        FROM evaluations
        WHERE fit_score IS NOT NULL
        GROUP BY range_label
        """,
    )
    counts = {r["range_label"]: int(r["count"]) for r in rows if r["range_label"] is not None}
    return [{"range_label": label, "count": counts.get(label, 0)} for label in _SCORE_BUCKET_LABELS]


async def _skill_gaps(session: AsyncSession) -> dict[str, Any]:
    present = await fetch_all(
        session,
        """
        SELECT skill, count(*) AS count
        FROM evaluations, jsonb_array_elements_text(matched_skills) AS skill
        GROUP BY skill
        ORDER BY count DESC, skill ASC
        LIMIT 10
        """,
    )
    missing = await fetch_all(
        session,
        """
        SELECT gap AS skill, count(*) AS count
        FROM evaluations, jsonb_array_elements_text(gaps) AS gap
        GROUP BY gap
        ORDER BY count DESC, gap ASC
        LIMIT 10
        """,
    )
    return {
        "top_present": [{"skill": r["skill"], "count": int(r["count"])} for r in present],
        "top_missing": [{"skill": r["skill"], "count": int(r["count"])} for r in missing],
    }


async def _usage(session: AsyncSession) -> list[dict[str, Any]]:
    """Grouped by provider (and model). `billable` marks mock/ollama rows
    as excluded from the daily hosted-request budget, mirroring exactly
    the `NOT IN ('mock', 'ollama')` predicate `throttle.budget_used_today`
    uses -- so this chart never contradicts the `/api/health` header
    strip's budget number (PRD note in the F6 dispatch)."""
    rows = await fetch_all(
        session,
        """
        SELECT
          coalesce(payload->>'provider', 'unknown') AS provider,
          coalesce(payload->>'model', 'unknown') AS model,
          count(*) AS request_count,
          avg(coalesce((payload->>'latency_ms')::numeric, 0)) AS avg_latency_ms,
          sum(coalesce((payload->>'input_tokens')::int, 0)) AS total_input_tokens,
          sum(coalesce((payload->>'output_tokens')::int, 0)) AS total_output_tokens
        FROM audit_log
        WHERE action = 'llm_call'
        GROUP BY provider, model
        ORDER BY request_count DESC
        """,
    )
    return [
        {
            "provider": r["provider"],
            "model": r["model"],
            "request_count": int(r["request_count"]),
            "avg_latency_ms": float(r["avg_latency_ms"] or 0),
            "total_input_tokens": int(r["total_input_tokens"] or 0),
            "total_output_tokens": int(r["total_output_tokens"] or 0),
            "billable": r["provider"] not in ("mock", "ollama"),
        }
        for r in rows
    ]


@router.get("/analytics")
async def get_analytics(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:  # noqa: B008
    """`{pipeline, score_distribution, skill_gaps, usage}` per the frozen
    `AnalyticsResponse` contract, plus additive `budget_used_today` /
    `budget_limit` fields (same pattern `app.main`'s `/api/health` and
    A6's `routers/jobs.py` use) so the usage panel can render the same
    "today's requests vs. free-tier budget" story `/api/health` shows."""
    pipeline = await _pipeline(session)
    score_distribution = await _score_distribution(session)
    skill_gaps = await _skill_gaps(session)
    usage = await _usage(session)
    used_today = await budget_used_today(session)

    return {
        "pipeline": pipeline,
        "score_distribution": score_distribution,
        "skill_gaps": skill_gaps,
        "usage": usage,
        "budget_used_today": used_today,
        "budget_limit": settings.llm_daily_budget,
    }


# ---------------------------------------------------------------------------
# POST /analytics/query
# ---------------------------------------------------------------------------


@router.post("/analytics/query")
async def query_analytics(
    body: AnalyticsQueryRequest, session: AsyncSession = Depends(get_session)  # noqa: B008
) -> dict[str, Any]:
    """`{question}` -> `{sql, rows, explanation}` per the frozen
    `AnalyticsQueryRequest`/`AnalyticsQueryResponse` contract. See module
    docstring for the full hostile-input treatment."""
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="`question` is required and must be a non-empty string")

    agent = get_agent(
        name="analytics_query",
        output_type=AnalyticsQueryPlan,
        system_prompt=SYSTEM_PROMPT,
        tools=None,
    )
    result = await agent.run(question)
    plan = result.output  # AnalyticsQueryPlan -- never a raw SQL string, see class docstring

    try:
        sql, params = build_sql(plan)
    except AnalyticsQueryError as exc:
        raise HTTPException(status_code=400, detail=f"Rejected query plan: {exc}") from exc

    try:
        rows = await _run_readonly(session, sql, params)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Query could not be executed read-only: {exc}") from exc

    return {"sql": sql, "rows": rows, "explanation": _explain(plan)}


__all__ = [
    "ALLOWLIST",
    "AnalyticsQueryError",
    "AnalyticsQueryPlan",
    "build_sql",
    "router",
]
