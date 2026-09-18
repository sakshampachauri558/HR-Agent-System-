"""F2/F5 — POST /resumes (upload), GET /resumes/stream (SSE). PRD §9 contract.

Upload flow: parse -> redact -> assign a pseudonym ("Candidate A", ...) ->
persist -> kick off evaluation in the background through a bounded pool of
`settings.eval_concurrency` (default 2), matching OpenRouter's free-tier
concurrency budget (PRD §10). Real identity lives only in `resumes.raw_text`
and is never sent to the model — only `redacted_text` / per-section
redacted text (stored in `resumes.sections`) ever reaches the evaluator.

`GET /resumes/{id}` is a small addition beyond the literal §9 contract
(not owned by anyone else) so a future UI can show the human the
unredacted resume next to the "scored on redacted text" badge (PRD §4 F2)
without inventing a new response shape — it just returns the existing
`Resume` model from `schemas.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from sse_starlette.sse import EventSourceResponse

from app import parse, redact
from app.agents.evaluator import evaluate_resume
from app.config import settings
from app.db import execute, fetch_all, fetch_one, session_scope
from app.schemas import Resume, ResumesUploadResponse, ResumeUploadItem

logger = logging.getLogger("peopleops.resumes")

router = APIRouter()

_eval_semaphore = asyncio.Semaphore(max(settings.eval_concurrency, 1))

# In-process association of resume -> job, and job -> SSE subscriber
# queues. `resumes` has no `job_id` column (frozen schema — a resume can
# in principle be evaluated against more than one job), so the board's
# per-job live stream is tracked here for the lifetime of this process.
# Good enough for a single-backend-process demo; lost on a reload/restart,
# same as any other purely in-memory pub/sub.
_job_resumes: dict[str, set[str]] = defaultdict(set)
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def _label_for_index(n: int) -> str:
    """Spreadsheet-column-style pseudonym: 0->A, 1->B, ..., 25->Z, 26->AA."""
    n += 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"Candidate {letters}"


async def _publish(job_id: UUID, resume_id: UUID, status: str, error: str | None) -> None:
    payload = {"resume_id": str(resume_id), "status": status, "error": error}
    for queue in list(_subscribers.get(str(job_id), ())):
        queue.put_nowait(payload)


async def _set_status(resume_id: UUID, job_id: UUID, status: str, error: str | None = None) -> None:
    async with session_scope() as session:
        await execute(
            session,
            "UPDATE resumes SET status = :status, error = :error WHERE id = :id",
            {"status": status, "error": error, "id": resume_id},
        )
    await _publish(job_id, resume_id, status, error)


async def _process_resume(resume_id: UUID, job_id: UUID, file_name: str, data: bytes) -> None:
    try:
        await _set_status(resume_id, job_id, "parsing")
        raw_text = parse.extract_text(file_name, data)
        sections_raw = parse.split_sections(raw_text)
        redacted_text = redact.redact(raw_text)
        sections_redacted = {key: redact.redact(value) for key, value in sections_raw.items()}

        async with session_scope() as session:
            await execute(
                session,
                """
                UPDATE resumes
                SET raw_text = :raw_text, redacted_text = :redacted_text, sections = :sections
                WHERE id = :id
                """,
                {
                    "raw_text": raw_text,
                    "redacted_text": redacted_text,
                    "sections": sections_redacted,
                    "id": resume_id,
                },
            )

        await _set_status(resume_id, job_id, "evaluating")
        async with _eval_semaphore:
            await evaluate_resume(resume_id, job_id)
        await _set_status(resume_id, job_id, "scored")
    except Exception as exc:
        # A bad upload must fail its own row, never take down the process.
        logger.exception("resume %s failed to process", resume_id)
        await _set_status(resume_id, job_id, "failed", error=str(exc)[:500])


@router.post("/resumes", response_model=ResumesUploadResponse)
async def upload_resumes(
    job_id: UUID = Form(...),  # noqa: B008 - required FastAPI form-field idiom
    files: list[UploadFile] = File(...),  # noqa: B008 - required FastAPI file-upload idiom
) -> ResumesUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    async with session_scope() as session:
        job = await fetch_one(session, "SELECT id FROM jobs WHERE id = :id", {"id": job_id})
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        count_row = await fetch_one(session, "SELECT count(*) AS n FROM resumes")
    base_index = int(count_row["n"]) if count_row else 0

    items: list[ResumeUploadItem] = []
    to_process: list[tuple[UUID, str, bytes]] = []

    for i, upload in enumerate(files):
        data = await upload.read()
        resume_id = uuid4()
        label = _label_for_index(base_index + i)
        async with session_scope() as session:
            await execute(
                session,
                """
                INSERT INTO resumes (id, candidate_label, file_name, status)
                VALUES (:id, :label, :file_name, 'queued')
                """,
                {"id": resume_id, "label": label, "file_name": upload.filename},
            )
        _job_resumes[str(job_id)].add(str(resume_id))
        items.append(ResumeUploadItem(id=resume_id, status="queued"))
        to_process.append((resume_id, upload.filename or "resume.txt", data))

    for resume_id, file_name, data in to_process:
        asyncio.create_task(_process_resume(resume_id, job_id, file_name, data))

    return ResumesUploadResponse(resumes=items)


@router.get("/resumes/stream")
async def resumes_stream(job_id: UUID, request: Request) -> EventSourceResponse:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers[str(job_id)].add(queue)

    async def event_generator():
        try:
            resume_ids = [UUID(rid) for rid in _job_resumes.get(str(job_id), ())]
            if resume_ids:
                async with session_scope() as session:
                    rows = await fetch_all(
                        session,
                        "SELECT id, status, error FROM resumes WHERE id = ANY(:ids)",
                        {"ids": resume_ids},
                    )
                for row in rows:
                    yield {
                        "event": "status",
                        "data": json.dumps(
                            {"resume_id": str(row["id"]), "status": row["status"], "error": row["error"]}
                        ),
                    }

            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield {"event": "status", "data": json.dumps(item)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            _subscribers[str(job_id)].discard(queue)

    return EventSourceResponse(event_generator())


@router.get("/resumes/{resume_id}", response_model=Resume)
async def get_resume(resume_id: UUID) -> Resume:
    async with session_scope() as session:
        row = await fetch_one(session, "SELECT * FROM resumes WHERE id = :id", {"id": resume_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return Resume(**row)
