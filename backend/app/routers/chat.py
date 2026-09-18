"""POST /api/chat — PRD §4 F1 / §10.

Retrieve (hybrid, see `app.rag.retrieve`) -> build a `<context>` block where
every chunk carries its `chunk_id` -> one `get_agent` call -> resolve the
model's cited chunk_ids against the DB -> return `ChatResponse`.

Owned by A4 (RAG Query). Router is registered WITHOUT the `/api` prefix;
`app.main` mounts it there.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.llm.provider import get_agent
from app.rag.retrieve import retrieve
from app.schemas import ChatRequest, ChatResponse, Citation, Turn

logger = logging.getLogger("peopleops.routers.chat")

router = APIRouter()

# PRD §4 F1: "Multi-turn: last 6 turns kept."
MAX_HISTORY_TURNS = 6

# Named in the refusal path per PRD §4/§10 ("say so and name the HR contact").
HR_CONTACT = "peopleops@company.example"

# Matches the `[^chunk_id]` footnote markers the model is instructed to put
# inline in `answer`, so we can cross-check them against the structured
# `citations` field even if the model only remembers one of the two.
_CITATION_REF_RE = re.compile(r"\[\^([0-9a-fA-F-]{8,36})\]")


# ---------------------------------------------------------------------------
# Generation prompt contract (PRD §10) -- two worked examples, one grounded
# answer and one refusal, because on a 70B free model the citation format
# holds only if it is demonstrated, not merely described.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are the PeopleOps policy assistant. You answer employee questions about \
company HR policy using ONLY the numbered chunks given to you inside <context>...</context>.

Hard rules:
- Never use outside knowledge about HR, employment law, or "how companies usually do this". If \
it is not written in <context>, you do not know it.
- After every sentence that states a fact from the context, cite the chunk it came from with an \
inline footnote marker in the form [^chunk_id], using the exact chunk_id shown in <context>.
- Also populate the structured `citations` field with one entry per chunk_id you cited, copying \
`document_title` and `section` from <context> and giving a short verbatim `quote` (not the whole \
chunk) plus its `start_char`/`end_char` from <context>.
- If <context> is empty, or none of the chunks actually answer the question asked, do not guess \
and do not stitch together a plausible-sounding answer from partial matches. Say plainly that it \
is not covered in the policies you have, set `grounded` to false, leave `citations` empty, and \
direct the person to {HR_CONTACT}.
- Never invent a chunk_id. Only ever cite ids that literally appear in <context>.

Worked example 1 -- grounded answer:
<context>
[chunk_id: 11111111-1111-1111-1111-111111111111]
Document: Leave Policy -- Section: 3.2 Casual Leave Carryover
Up to 5 unused casual leave days may carry forward into the next calendar year with manager \
approval.

[chunk_id: 22222222-2222-2222-2222-222222222222]
Document: Leave Policy -- Section: 3.3 Use-It-By Window
Carried-over casual leave days must be used within the first quarter of the following year or \
they are forfeited.
</context>
Question: How many casual leaves carry forward?
Response:
{{
  "answer": "Employees accrue 12 casual leave days per year, and up to 5 unused casual leave days \
carry forward into the next calendar year with manager approval [^11111111-1111-1111-1111-111111111111]. \
Carried-over days must be used within the first quarter of the new year or they are forfeited \
[^22222222-2222-2222-2222-222222222222].",
  "citations": [
    {{"chunk_id": "11111111-1111-1111-1111-111111111111", "document_title": "Leave Policy", \
"section": "3.2 Casual Leave Carryover", "quote": "Up to 5 unused casual leave days may carry \
forward into the next calendar year with manager approval.", "start_char": 420, "end_char": 522}},
    {{"chunk_id": "22222222-2222-2222-2222-222222222222", "document_title": "Leave Policy", \
"section": "3.3 Use-It-By Window", "quote": "Carried-over casual leave days must be used within \
the first quarter of the following year or they are forfeited.", "start_char": 523, "end_char": 640}}
  ],
  "grounded": true, "provider": "mock", "model": "mock-fixture-v1"
}}

Worked example 2 -- refusal (context does not cover the question):
<context>
(empty -- no chunk in the policy corpus discusses contractor work-from-home arrangements)
</context>
Question: What's the WFH policy for contractors?
Response:
{{
  "answer": "That isn't covered in the policies I have -- here's who to ask: reach out to \
{HR_CONTACT} about contractor work-from-home arrangements.",
  "citations": [], "grounded": false, "provider": "mock", "model": "mock-fixture-v1"
}}

Always respond with a single JSON object matching this shape exactly: answer, citations, \
grounded, provider, model."""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _build_context_block(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "(empty -- no policy chunk was retrieved for this question)"
    parts = []
    for c in chunks:
        section = c.get("section") or "(no section)"
        parts.append(
            f"[chunk_id: {c['chunk_id']}]\n"
            f"Document: {c.get('document_title', 'Untitled document')} -- Section: {section}\n"
            f"{c['text']}"
        )
    return "\n\n".join(parts)


def _build_user_prompt(
    question: str,
    standalone_query: str,
    history: list[Turn],
    context_block: str,
) -> str:
    history_block = "\n".join(f"{t.role}: {t.content}" for t in history) or "(none)"
    rewritten_note = "" if standalone_query == question else f"(interpreted as: {standalone_query})\n"
    return (
        f"<context>\n{context_block}\n</context>\n\n"
        f"Conversation history (most recent last):\n{history_block}\n\n"
        f"{rewritten_note}Question: {question}"
    )


def _chunk_dep(chunk: dict[str, Any]) -> dict[str, Any]:
    """Shape mock.py's `_coerce_citation` expects, so `LLM_PROVIDER=mock`'s
    `chat_answer` fixture swaps its illustrative citations for these real,
    retrieved ones -- letting the citation-chip -> source-drawer flow be
    exercised against real seeded documents instead of placeholder UUIDs."""
    return {
        "chunk_id": str(chunk["chunk_id"]),
        "document_title": chunk.get("document_title") or "Untitled document",
        "section": chunk.get("section") or "",
        "quote": chunk.get("text") or "",
        "start_char": chunk.get("start_char") or 0,
        "end_char": chunk.get("end_char") or 0,
    }


# ---------------------------------------------------------------------------
# Citation resolution — never return a citation that doesn't resolve to a
# real chunk. A citation that looks authoritative and points nowhere is
# worse than no citation at all.
#
# FIX-1: resolution is deliberately scoped to `chunk_lookup` -- the chunks
# that survived retrieval's relevance floor for THIS question (see
# `app.rag.retrieve.VEC_MAX_DISTANCE`) -- and nothing else. An earlier
# version fell back to a DB-wide lookup by chunk_id when a candidate id
# wasn't in `chunk_lookup`, which meant any well-formed UUID that happened
# to name a real chunk ANYWHERE in the corpus (a stale id from history, a
# copy-paste from a different section, a lucky guess) would resolve and
# count toward `grounded=True`. A citation being "a real chunk that exists"
# is not the same claim as "the chunk this query actually retrieved as
# relevant" -- only the latter is legitimate grounding evidence. Dropping
# the fallback means a citation is only ever accepted if it points at a
# chunk this specific question's retrieval pass judged relevant enough to
# pass the floor.
# ---------------------------------------------------------------------------


def _candidate_chunk_ids(output: ChatResponse) -> list[str]:
    ids: list[str] = []
    for c in output.citations:
        cid = str(c.chunk_id)
        if cid not in ids:
            ids.append(cid)
    for m in _CITATION_REF_RE.finditer(output.answer or ""):
        cid = m.group(1)
        if cid not in ids:
            ids.append(cid)
    return ids


def _resolve_citations(
    candidate_ids: list[str],
    chunk_lookup: dict[str, dict[str, Any]],
) -> list[Citation]:
    resolved: list[Citation] = []
    seen: set[str] = set()
    for raw_id in candidate_ids:
        try:
            cid = str(UUID(str(raw_id)))
        except (ValueError, AttributeError, TypeError):
            continue  # not even a well-formed UUID -- can't be a real chunk_id
        if cid in seen:
            continue
        seen.add(cid)

        row = chunk_lookup.get(cid)
        if row is None:
            logger.info(
                "dropping citation for chunk_id %s -- not among this question's "
                "retrieved (post-floor) chunks",
                cid,
            )
            continue

        resolved.append(
            Citation(
                chunk_id=row["chunk_id"],
                document_title=row["document_title"],
                section=row.get("section") or "",
                quote=row["text"],
                start_char=row.get("start_char") or 0,
                end_char=row.get("end_char") or 0,
            )
        )
    return resolved


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, session: SessionDep) -> ChatResponse:
    history = payload.history[-MAX_HISTORY_TURNS:]
    standalone_query, chunks = await retrieve(session, payload.question, history)

    context_block = _build_context_block(chunks)
    user_prompt = _build_user_prompt(payload.question, standalone_query, history, context_block)

    # Under `LLM_PROVIDER=mock` this selects which fixture answers the
    # call. No retrieved chunks means there is nothing to ground an answer
    # on, so we go straight to the refusal fixture/behavior rather than
    # asking the model to invent something from an empty context.
    fixture_name = "chat_answer" if chunks else "chat_refusal"
    agent = get_agent(
        name=fixture_name,
        output_type=ChatResponse,
        system_prompt=SYSTEM_PROMPT,
        tools=None,
    )

    deps: dict[str, Any] | None = {"chunks": [_chunk_dep(c) for c in chunks]} if chunks else None
    result = await agent.run(user_prompt, deps=deps)
    output: ChatResponse = result.output

    chunk_lookup = {str(c["chunk_id"]): c for c in chunks}
    resolved_citations = _resolve_citations(_candidate_chunk_ids(output), chunk_lookup)

    # A "grounded" answer requires at least one citation that actually
    # resolved to a real chunk; otherwise it's a refusal regardless of what
    # the model's own `grounded` flag claimed.
    grounded = bool(output.grounded) and bool(resolved_citations)

    return ChatResponse(
        answer=output.answer,
        citations=resolved_citations,
        grounded=grounded,
        provider=output.provider,
        model=output.model,
    )
