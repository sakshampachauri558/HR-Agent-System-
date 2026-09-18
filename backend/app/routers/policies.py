"""F1 ingestion endpoints -- upload, list, delete policy documents.

Owned by A3. Mounted under `/api` by `app/main.py`'s router discovery, so
these are served at `/api/policies` and `/api/policies/{document_id}`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import execute, fetch_all, fetch_one, get_session
from app.rag.ingest import IngestError, ingest_document
from app.schemas import PolicyUploadResponse

router = APIRouter()

# IngestError.code -> HTTP status. The JSON `error.code` field the client
# actually sees comes from app/main.py's shared HTTPException handler
# (always "http_error", since that file is frozen and only special-cases
# its own LLMRateLimited/BudgetExhausted types) -- the status code and the
# message text are what carry the specific reason here.
_STATUS_BY_CODE = {
    "unsupported_file_type": 415,
    "unparseable_file": 422,
    "file_too_large": 413,
    "empty_file": 422,
}


@router.post("/policies", response_model=PolicyUploadResponse)
async def upload_policy(
    file: UploadFile = File(...),  # noqa: B008 - idiomatic FastAPI; no repo-wide ruff config allowlists it
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PolicyUploadResponse:
    raw = await file.read()
    try:
        result = await ingest_document(
            session,
            filename=file.filename or "upload",
            content_type=file.content_type,
            raw=raw,
        )
    except IngestError as exc:
        status_code = _STATUS_BY_CODE.get(exc.code, 422)
        raise HTTPException(status_code=status_code, detail=f"[{exc.code}] {exc}") from exc

    return PolicyUploadResponse(document_id=result["document_id"], chunk_count=result["chunk_count"])


@router.get("/policies")
async def list_policies(session: AsyncSession = Depends(get_session)) -> dict:  # noqa: B008
    """Returns `{"documents": [...]}`. Each document carries `chunk_count`
    in addition to the frozen `Document` schema's fields (title, kind,
    source_name, char_count, uploaded_at) -- returned as a plain dict
    (no `response_model`) specifically so that extra, additive field isn't
    stripped."""
    rows = await fetch_all(
        session,
        """
        SELECT d.id, d.title, d.kind, d.source_name, d.char_count, d.uploaded_at,
               COUNT(c.id)::int AS chunk_count
        FROM documents d
        LEFT JOIN chunks c ON c.document_id = d.id
        GROUP BY d.id
        ORDER BY d.uploaded_at DESC
        """,
    )
    documents = [
        {
            "id": str(row["id"]),
            "title": row["title"],
            "kind": row["kind"],
            "source_name": row["source_name"],
            "char_count": row["char_count"],
            "uploaded_at": row["uploaded_at"].isoformat(),
            "chunk_count": row["chunk_count"],
        }
        for row in rows
    ]
    return {"documents": documents}


@router.delete("/policies/{document_id}")
async def delete_policy(document_id: UUID, session: AsyncSession = Depends(get_session)) -> dict:  # noqa: B008
    """Cascade-deletes the document's chunks too (`chunks.document_id ...
    ON DELETE CASCADE` in `db/init.sql`) -- useful for re-ingesting a
    corrected file during the demo."""
    existing = await fetch_one(session, "SELECT id FROM documents WHERE id = :id", {"id": document_id})
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Policy document {document_id} not found.")

    await execute(session, "DELETE FROM documents WHERE id = :id", {"id": document_id})
    return {"deleted": True, "document_id": str(document_id)}
