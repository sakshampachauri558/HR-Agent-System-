"""F2 — POST /evaluate, GET /evaluations/{id}. PRD §9 contract.

Thin HTTP layer over `app.agents.evaluator`. `POST /evaluate` is
idempotent per the `UNIQUE (resume_id, job_id)` constraint on
`evaluations`: if a row already exists it is returned as-is, with no LLM
call spent re-scoring it (the daily budget is the binding constraint —
PRD §10 — so an idempotent re-request must be free).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.agents.evaluator import evaluate_resume, row_to_evaluation_result
from app.db import fetch_one, session_scope
from app.schemas import EvaluateRequest, EvaluationResult

router = APIRouter()


@router.post("/evaluate", response_model=EvaluationResult)
async def create_evaluation(body: EvaluateRequest) -> EvaluationResult:
    async with session_scope() as session:
        resume = await fetch_one(session, "SELECT id FROM resumes WHERE id = :id", {"id": body.resume_id})
        job = await fetch_one(session, "SELECT id FROM jobs WHERE id = :id", {"id": body.job_id})
        if resume is None:
            raise HTTPException(status_code=404, detail="Resume not found")
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")

        existing = await fetch_one(
            session,
            "SELECT * FROM evaluations WHERE resume_id = :r AND job_id = :j",
            {"r": body.resume_id, "j": body.job_id},
        )
    if existing is not None:
        return row_to_evaluation_result(existing)

    return await evaluate_resume(body.resume_id, body.job_id)


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationResult)
async def get_evaluation(evaluation_id: UUID) -> EvaluationResult:
    async with session_scope() as session:
        row = await fetch_one(session, "SELECT * FROM evaluations WHERE id = :id", {"id": evaluation_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return row_to_evaluation_result(row)
