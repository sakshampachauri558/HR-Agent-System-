"""Parse -> chunk -> embed -> persist pipeline for policy documents (F1).

Owned by A3. `POST /api/policies` (`app/routers/policies.py`) is the only
caller in this build; kept as a standalone async function (rather than
inlined in the router) so a smoke-test script can call it directly against
an open DB session too.
"""

from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import execute
from app.rag.chunk import chunk_document
from app.rag.embed import embed_texts

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB -- generous for a policy PDF, cheap to enforce

_CONTENT_TYPE_TO_KIND = {
    "application/pdf": "pdf",
    "text/markdown": "md",
    "text/x-markdown": "md",
    "text/plain": "txt",
}


class IngestError(ValueError):
    """Raised for a rejected upload (bad type, empty/unparseable content).

    `app/routers/policies.py` maps `.code` to an appropriate 4xx status so
    a bad upload never falls through to a generic 500.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _kind_from_name(filename: str, content_type: str | None) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".md", ".markdown")):
        return "md"
    if lower.endswith(".txt"):
        return "txt"
    if content_type and content_type in _CONTENT_TYPE_TO_KIND:
        return _CONTENT_TYPE_TO_KIND[content_type]
    raise IngestError(
        "unsupported_file_type",
        f"Unsupported file type for '{filename}'. Upload a .pdf, .md, or .txt file.",
    )


def _parse_pdf(raw: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise IngestError("unparseable_file", f"Could not parse PDF: {exc}") from exc
    text = "\n\n".join(pages).strip()
    if not text:
        raise IngestError(
            "unparseable_file",
            "No extractable text found in this PDF (it may be a scanned image with no text layer).",
        )
    return text


def _parse_text(raw: bytes, filename: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception as exc:
            raise IngestError("unparseable_file", f"Could not decode '{filename}' as text.") from exc
    text = text.strip()
    if not text:
        raise IngestError("unparseable_file", f"'{filename}' is empty.")
    return text


def parse_document(filename: str, content_type: str | None, raw: bytes) -> tuple[str, str]:
    """Returns `(kind, plain_text)`. `kind` is one of `pdf`/`md`/`txt`
    (matches `documents.kind` in `db/init.sql`)."""
    kind = _kind_from_name(filename, content_type)
    text = _parse_pdf(raw) if kind == "pdf" else _parse_text(raw, filename)
    return kind, text


def _title_from_filename(filename: str) -> str:
    stem = Path(filename or "policy").stem.replace("_", " ").replace("-", " ").strip()
    return stem.title() if stem else "Untitled Policy"


async def ingest_document(
    session: AsyncSession,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    raw: bytes | None = None,
    title: str | None = None,
    kind: str | None = None,
    source_name: str | None = None,
    text: str | None = None,
) -> dict[str, str | int]:
    """Parse -> chunk -> embed -> persist one policy document.

    Two calling conventions, both keyword-only:

    - Upload path (`routers/policies.py`, unchanged contract): `filename` +
      `content_type` + `raw` bytes -- parsed here via `parse_document`.
    - Pre-parsed-text path (`scripts.seed`'s tier-1 introspection, see its
      `_try_real_ingest`): `title` + `kind` + `source_name` + `text`,
      already-decoded markdown/plain text, nothing to parse. This is what
      lets `scripts.seed` exercise this module's real chunk+embed pipeline
      (FIX-3's clause-level chunking) instead of always falling back to
      its own inline whole-section chunker -- `scripts.seed` already tries
      exactly this shape via `inspect.signature`; it just had no matching
      parameter names to bind before this change.

    Exactly one of `raw` or `text` must be supplied. Returns
    `{"document_id": str, "chunk_count": int}` (PRD §9
    `PolicyUploadResponse` shape, and also what `scripts.seed`'s
    `_extract_chunk_count` recognizes). Raises `IngestError` for a bad
    upload -- the router maps that to a 4xx, never a 500.
    """
    if raw is not None:
        if not raw:
            raise IngestError("empty_file", f"'{filename or 'upload'}' is empty.")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise IngestError(
                "file_too_large",
                f"'{filename}' exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit.",
            )
        resolved_kind, resolved_text = parse_document(filename or "upload", content_type, raw)
        resolved_title = title or _title_from_filename(filename or "upload")
        resolved_source_name = source_name or filename or "upload"
    elif text is not None:
        if not text.strip():
            raise IngestError("empty_file", f"'{source_name or title or 'document'}' is empty.")
        resolved_kind = kind or "md"
        resolved_text = text
        resolved_title = title or _title_from_filename(source_name or title or "policy")
        resolved_source_name = source_name or filename or resolved_title
    else:
        raise IngestError("empty_file", "ingest_document() needs either `raw` bytes or pre-parsed `text`.")

    chunks = chunk_document(resolved_text, title=resolved_title)
    if not chunks:
        raise IngestError("unparseable_file", f"No chunkable content found in '{resolved_source_name}'.")

    # Batched: one embed_texts() call for every chunk, one INSERT for the
    # document, one executemany-style INSERT for all chunks -- not one
    # round trip per chunk, which would be visibly slow on a real policy PDF.
    #
    # Embeds `c.embed_text` (FIX-3's query-alignment enrichment: heading +
    # salient keywords ahead of the clause), NOT `c.text` -- `c.text` is
    # what gets stored in `chunks.text` below and is what the citation
    # drawer displays verbatim, so it must stay exactly the real document
    # content. Only the *embedded* representation is enriched; the
    # asymmetric passage/query embedding split in `embed.py` is unaffected
    # by this -- both still go through `passage_embed`.
    vectors = embed_texts([c.embed_text for c in chunks])

    document_id = uuid4()
    await execute(
        session,
        """
        INSERT INTO documents (id, title, kind, source_name, char_count, uploaded_at)
        VALUES (:id, :title, :kind, :source_name, :char_count, now())
        """,
        {
            "id": document_id,
            "title": resolved_title,
            "kind": resolved_kind,
            "source_name": resolved_source_name,
            "char_count": len(resolved_text),
        },
    )

    rows = [
        {
            "id": uuid4(),
            "document_id": document_id,
            "section": c.section,
            "ordinal": c.ordinal,
            "text": c.text,
            "start_char": c.start_char,
            "end_char": c.end_char,
            "token_count": c.token_count,
            "embedding": vector,
        }
        for c, vector in zip(chunks, vectors, strict=True)
    ]
    await execute(
        session,
        """
        INSERT INTO chunks
            (id, document_id, section, ordinal, text, start_char, end_char, token_count, embedding)
        VALUES
            (:id, :document_id, :section, :ordinal, :text, :start_char, :end_char, :token_count, :embedding)
        """,
        rows,
    )

    return {"document_id": str(document_id), "chunk_count": len(rows)}
