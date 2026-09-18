"""F3 — Jobs / JD Studio routes.

    POST /jobs           JobDraft -> generates the JD (one LLM call, see
                          app.agents.jd), persists it, returns {job, ...}.
    GET  /jobs            -> {jobs: [...]}
    GET  /jobs/{id}        -> {job, evaluations}

Mounted under /api by app/main.py's router discovery (this module exposes
`router = APIRouter()` with routes registered WITHOUT the /api prefix).

The frozen contract (`schemas.py`) types POST /jobs's response as
`JobCreateResponse = {job: Job}` and GET /jobs/{id}'s as
`JobDetailResponse = {job: Job, evaluations: list[EvaluationResult]}`. Both
routes here return that shape plus a couple of additive fields the frozen
`Job`/`EvaluationResult` models have no room for but the frontend (which
this same agent owns) needs:
  - POST /jobs also returns top-level `inclusive_language_flags` — the
    generated JD's bias-language review, so `NewJob.tsx` can render it
    inline without a second round trip.
  - GET /jobs also annotates each job with `candidate_count` (evaluations
    written against it — the only resume<->job link the schema exposes,
    since `resumes` carries no `job_id` column).
  - GET /jobs/{id}'s `evaluations` entries carry `candidate_label` (joined
    from `resumes`) alongside the full evaluation row, because A8's board
    needs a human-readable label and `EvaluationResult` doesn't have one.
A client that only reads the documented fields is unaffected; this is the
same "extra, additive fields" pattern `main.py` uses for `/api/health`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.jd import generate_jd
from app.db import fetch_all, fetch_one, get_session
from app.schemas import DEFAULT_RUBRIC, Job, JobDraft, RubricCriterion

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rubric_payload(rubric_weights: list[RubricCriterion] | None) -> list[dict[str, Any]]:
    """Default to DEFAULT_RUBRIC (schemas.py) when the caller doesn't
    supply their own weights, per PRD §4 F3 / the dispatch spec."""
    weights = rubric_weights if rubric_weights else DEFAULT_RUBRIC
    return [w.model_dump() for w in weights]


def _job_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    """`SELECT * FROM jobs` returns exactly the frozen `Job` columns plus,
    in the list query, an extra `candidate_count` — Pydantic v2 ignores
    unknown kwargs by default, so `Job(**row)` validates either way; we
    re-attach `candidate_count` afterwards if the row carried one."""
    job = Job(**row)
    payload = job.model_dump(mode="json")
    if "candidate_count" in row:
        payload["candidate_count"] = int(row["candidate_count"] or 0)
    return payload


def _evaluation_row_to_board_item(row: dict[str, Any]) -> dict[str, Any]:
    """One board-row-shaped entry for A8's ranking board: the fields the
    dispatch spec calls out by name (id, resume_id, candidate_label,
    fit_score, recommendation, matched_skills, gaps), plus the rest of the
    `evaluations` columns so a consumer that wants the fuller
    `EvaluationResult` shape (criteria/summary/meta) has them too."""
    return {
        "id": str(row["id"]),
        "resume_id": str(row["resume_id"]),
        "job_id": str(row["job_id"]),
        "candidate_label": row.get("candidate_label"),
        "fit_score": row.get("fit_score"),
        "recommendation": row.get("recommendation"),
        "matched_skills": row.get("matched_skills") or [],
        "gaps": row.get("gaps") or [],
        "criteria": row.get("criteria") or [],
        "summary": row.get("summary"),
        "meta": {
            "provider": row.get("provider"),
            "model": row.get("model"),
            "rubric_version": row.get("rubric_version"),
            "prompt_version": row.get("prompt_version"),
            "input_tokens": row.get("input_tokens") or 0,
            "output_tokens": row.get("output_tokens") or 0,
            "latency_ms": row.get("latency_ms") or 0,
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/jobs")
async def create_job(
    job_draft: JobDraft, session: AsyncSession = Depends(get_session)  # noqa: B008 - FastAPI DI pattern
) -> dict[str, Any]:
    """`JobDraft` -> generates the JD (one LLM request, see
    app.agents.jd.generate_jd), persists it, returns `{job, ...}`.

    FastAPI validates the body into `JobDraft` itself; a malformed body
    422s through `main.py`'s `RequestValidationError` handler, which
    already emits the `{"error": {"code","message"}}` envelope.
    """
    generated = await generate_jd(job_draft)

    title = job_draft.title or generated.title
    level = job_draft.level or generated.level
    department = job_draft.department or generated.department
    location = job_draft.location or generated.location
    must_haves = job_draft.must_haves or generated.must_haves
    nice_to_haves = job_draft.nice_to_haves or generated.nice_to_haves
    min_years = job_draft.min_years if job_draft.min_years is not None else generated.min_years
    comp_min = job_draft.comp_min if job_draft.comp_min is not None else generated.comp_min
    comp_max = job_draft.comp_max if job_draft.comp_max is not None else generated.comp_max
    rubric_weights = _rubric_payload(job_draft.rubric_weights)

    row = await fetch_one(
        session,
        """
        INSERT INTO jobs (
            title, level, location, department, description_md,
            must_haves, nice_to_haves, min_years, comp_min, comp_max,
            rubric_weights
        ) VALUES (
            :title, :level, :location, :department, :description_md,
            :must_haves, :nice_to_haves, :min_years, :comp_min, :comp_max,
            :rubric_weights
        )
        RETURNING id, title, level, location, department, description_md,
                  must_haves, nice_to_haves, min_years, comp_min, comp_max,
                  rubric_weights, created_at
        """,
        {
            "title": title,
            "level": level,
            "location": location,
            "department": department,
            "description_md": generated.description_md,
            "must_haves": must_haves,
            "nice_to_haves": nice_to_haves,
            "min_years": min_years,
            "comp_min": comp_min,
            "comp_max": comp_max,
            "rubric_weights": rubric_weights,
        },
    )
    if row is None:  # pragma: no cover - INSERT ... RETURNING always returns a row
        raise RuntimeError("INSERT INTO jobs did not return a row")

    return {
        "job": _job_row_to_payload(row),
        "inclusive_language_flags": [f.model_dump() for f in generated.inclusive_language_flags],
    }


@router.get("/jobs")
async def list_jobs(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:  # noqa: B008
    rows = await fetch_all(
        session,
        """
        SELECT j.*, COUNT(e.id) AS candidate_count
        FROM jobs j
        LEFT JOIN evaluations e ON e.job_id = j.id
        GROUP BY j.id
        ORDER BY j.created_at DESC
        """,
    )
    return {"jobs": [_job_row_to_payload(row) for row in rows]}


@router.get("/jobs/{id}")
async def get_job(id: UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:  # noqa: B008
    job_row = await fetch_one(session, "SELECT * FROM jobs WHERE id = :id", {"id": id})
    if job_row is None:
        raise HTTPException(status_code=404, detail=f"Job {id} not found")

    eval_rows = await fetch_all(
        session,
        """
        SELECT e.*, r.candidate_label
        FROM evaluations e
        JOIN resumes r ON r.id = e.resume_id
        WHERE e.job_id = :job_id
        ORDER BY e.fit_score DESC NULLS LAST, e.created_at ASC
        """,
        {"job_id": id},
    )

    return {
        "job": _job_row_to_payload(job_row),
        "evaluations": [_evaluation_row_to_board_item(row) for row in eval_rows],
    }
