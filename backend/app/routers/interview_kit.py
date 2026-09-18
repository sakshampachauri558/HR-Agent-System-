"""F4 — POST /interview-kit. PRD §9 contract.

Thin HTTP layer over `app.agents.interview_kit`. Idempotent: `interview_kits`
has no unique constraint on `evaluation_id` (frozen `db/init.sql`), so this
router is the one place enforcing "one kit per evaluation" -- it checks for
an existing row first and returns it unchanged, with zero LLM spend, rather
than ever regenerating on a repeat click (PRD §10's daily request budget is
the binding constraint on this whole app).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.agents.interview_kit import generate_interview_kit
from app.db import fetch_one, session_scope
from app.schemas import InterviewKitRequest, InterviewKitResponse, InterviewQuestion

router = APIRouter()


@router.post("/interview-kit", response_model=InterviewKitResponse)
async def create_interview_kit(body: InterviewKitRequest) -> InterviewKitResponse:
    async with session_scope() as session:
        evaluation = await fetch_one(
            session, "SELECT id FROM evaluations WHERE id = :id", {"id": body.evaluation_id}
        )
        if evaluation is None:
            raise HTTPException(status_code=404, detail="Evaluation not found")

        existing = await fetch_one(
            session,
            "SELECT questions FROM interview_kits WHERE evaluation_id = :id ORDER BY created_at ASC LIMIT 1",
            {"id": body.evaluation_id},
        )
    if existing is not None:
        return InterviewKitResponse(questions=[InterviewQuestion(**q) for q in (existing["questions"] or [])])

    return await generate_interview_kit(body.evaluation_id)
