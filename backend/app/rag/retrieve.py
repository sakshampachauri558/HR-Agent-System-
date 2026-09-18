"""Hybrid retrieval — PRD §10 / §4 F1.

    rewrite(question, history) -> standalone query
    vec   = SELECT ... ORDER BY embedding <=> :qvec LIMIT 20   -- pgvector cosine
    fts   = SELECT ... ORDER BY ts_rank_cd(tsv, plainto_tsquery(:q)) DESC LIMIT 20
    fused = RRF(vec, fts, k=60)[:8]
    final = rerank(query, fused)[:5]     -- ONLY if settings.rag_rerank (default False)

Owned by A4 (RAG Query). `backend/app/routers/chat.py` is the only expected
caller.

Wave-1 seam note: `app.rag.embed` is owned by A3 and may not exist yet
while this module was written in parallel (per the dispatch brief, A4 does
not wait for A3). The import below is guarded; if `embed_query` is
unavailable, or raises, or returns `None`, the vector leg of retrieval is
skipped and this module falls back to full-text (FTS) search alone, so
`/api/chat` stays exercisable end to end regardless of ingest progress.

Request-budget note (PRD §10): query rewriting and reranking each cost an
extra LLM request. Rewriting is skipped entirely (zero calls) when there is
no conversation history — the common case, so a first question costs
exactly one call (the generation call in `routers/chat.py`). Reranking is
gated behind `settings.rag_rerank`, which defaults to `False` and must stay
that way; flipping it on burns a third of every question's request budget
for a marginal recall gain.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import fetch_all
from app.llm.provider import get_agent

logger = logging.getLogger("peopleops.rag.retrieve")

try:
    from app.rag.embed import embed_query  # owned by A3 — may not exist yet

    _EMBED_AVAILABLE = True
except ImportError:
    embed_query = None  # type: ignore[assignment]
    _EMBED_AVAILABLE = False
    logger.warning(
        "app.rag.embed.embed_query not importable yet (A3 still landing ingest) "
        "-- retrieval falls back to keyword-only (Postgres FTS) search"
    )

# ---------------------------------------------------------------------------
# Tunables (PRD §10 / §4 F1)
# ---------------------------------------------------------------------------

VEC_LIMIT = 20
FTS_LIMIT = 20
RRF_K = 60
FUSED_TOP_N = 8
FINAL_TOP_N = 5

# ---------------------------------------------------------------------------
# Relevance floor (FIX-1 — PRD §4 F1 refusal path)
#
# pgvector's `<=>` is cosine DISTANCE (0 = identical, 2 = opposite; lower is
# closer). Nearest-neighbour search always returns *something* as long as
# the table is non-empty -- there is no built-in notion of "nothing here is
# actually relevant." Left unfiltered, an out-of-corpus question (e.g. "WFH
# policy for contractors?" against a corpus that never mentions contractors)
# still gets handed the least-bad chunk as if it answered the question. That
# chunk then gets summarized by the model in an authoritative voice, with a
# real, resolvable citation attached -- confidently wrong plus well-cited,
# the most damaging output this feature can produce (PRD §4 F1, §14 step 4).
#
# VEC_MAX_DISTANCE discards any chunk whose cosine distance to the query is
# >= this value *before* RRF fusion, so "nothing relevant" collapses to an
# empty vector leg instead of a top-20 of irrelevant chunks.
#
# --- Round 1 measurement: seed/questions.json only (A7's 12) -------------
# 10 in-corpus questions' distance to their own answering chunk: 0.0999 to
# 0.2194. 2 out-of-corpus questions' distance to their nearest chunk in the
# whole corpus: 0.3135, 0.3519. Clean gap of ~0.094, and 0.27 sat at the
# midpoint. This passed smoke 12/12 -- and was still wrong, because it was
# tuned only against A7's phrasing, which sits close to the source
# document's own wording. A real employee does not talk like the policy
# document.
#
# --- Round 2 measurement: + PRD §14's own demo lines + natural paraphrases
# Re-measured with PRD §14's two demo questions verbatim, plus ~20 casual
# paraphrases of the seeded questions and additional out-of-corpus probes
# phrased the same casual way (see the coordinator's re-tune request for
# the full list; not committed to seed/questions.json -- A7 owns that
# file). Distance to the answering chunk moves a LOT with phrasing alone,
# same corpus, same chunk, same embedding model:
#
#   "How many casual leave days can carry forward ... and by when must
#    they be used?" (A7's wording)            -> 0.0999
#   "How many casual leaves carry forward?"    (PRD §14's own wording,
#    same clause, same intent)                -> 0.3195
#
# That 0.20 swing from rewording alone is the real finding. Once natural
# phrasing is in the tuning set, the in-corpus and out-of-corpus distance
# distributions OVERLAP. Concretely, and this is the load-bearing number:
#
#   A7's own seeded refusal question "Does the company reimburse
#   relocation expenses ...?" (which `scripts.smoke` requires to refuse)
#   measures 0.3135 -- CLOSER than PRD §14's own "How many casual leaves
#   carry forward?" (0.3195), which must answer. 0.3135 < 0.3195.
#
# No single absolute cosine-distance cutoff can put both numbers on the
# correct side: any threshold high enough to keep 0.3195 as "answer" also
# keeps 0.3135 as "answer", breaking the mandatory refusal. This is a
# mathematical impossibility for this signal, not an under-tuned constant.
# It reproduces with more of the same casual phrasing, not just this one
# pair (e.g. "Do I need a doctor's note if I'm out sick?" at 0.3271, a
# clearly in-corpus question, is farther than "Can I expense my home gym
# equipment?" at 0.3235, which is genuinely out of corpus).
#
# Two alternative single-number signals were measured against the same
# widened set and rejected for the same reason (real reversals, not
# tuning slack):
#   - rank1->rank2 gap (a "relative" floor): out-of-corpus "gym equipment"
#     has a gap of 0.077, bigger than several genuinely-grounded questions
#     (e.g. "casual leaves carry forward" demo wording: 0.081 is close,
#     "casual leaves do I get a year" paraphrase: 0.012 is SMALLER).
#   - z-score of the top hit against the mean/stddev of that query's
#     distance to every chunk in the corpus (a query-relative floor): on
#     the first ~17-question widened set it happened not to reverse
#     (closest miss: refusal "gym equipment" z=2.61 vs. grounded "WFH
#     days/week" z=2.62 -- a 0.01 margin, thinner than the 0.036 margin
#     that triggered this re-tune). Adding 6 more casual-phrasing probes
#     broke it outright: grounded "Can I take my laptop home?" (z=2.14)
#     sits BELOW refusal "bringing my dog to the office?" (z=2.19) and
#     well below refusal "gym equipment" (z=2.61) and "parking ticket"
#     (z=2.44). More probes made the overlap worse, not better -- the
#     opposite of what you'd expect from a signal with real separating
#     power. Rejected.
#   - Requiring Postgres FTS agreement for borderline vector hits: dead on
#     arrival. `plainto_tsquery` ANDs every lexeme in the question, so
#     natural phrasing almost never satisfies it against prose it doesn't
#     quote -- 0 FTS rows on 16 of these 17 widened-set questions
#     (grounded AND refusal alike), vs. only 1/12 on A7's document-close
#     wording. FTS has essentially no opinion on a casually-phrased
#     question either way, so it cannot rescue a borderline vector call.
#
# CONCLUSION: no retrieval-distance-only signal (absolute, relative-gap,
# z-score, or FTS-agreement) cleanly separates realistic natural-language
# in-corpus questions from out-of-corpus ones on this corpus + this
# embedding model (BAAI/bge-small-en-v1.5, whole-section chunks). The
# overlap is real, not a fitting artifact -- reported as a known
# limitation rather than papered over with a threshold that "passes":
#   - The two mandatory `scripts.smoke` assertions (A7's 12 questions,
#     document-adjacent phrasing) are NOT in the overlap zone and stay
#     reliably correct at any threshold in (0.2194, 0.3135).
#   - PRD §14's own casual demo phrasing for the carry-forward question
#     (0.3195) falls just past the safe side of that same window and
#     will still incorrectly refuse under any threshold that keeps A7's
#     "relocation" refusal correct -- fixable on stage today only by the
#     presenter using wording closer to the source clause (A7's own
#     tested phrasing lands at 0.0999), not by a code change here.
#   - The real fix is upstream, likely A3/A7 territory: whole-`##`-
#     section chunks (~800 tokens) make a short casual query about one
#     narrow fact compete on roughly equal footing with several sibling
#     sections on the same broad topic (e.g. "Casual Leave" 0.1522 vs.
#     "Casual Leave Carryover" 0.0999 for a carryover question -- both
#     plausible). A per-chunk synthetic question/keyword embedding
#     (embed a short "what this chunk answers" string alongside the raw
#     section text, a standard fix for short-query/long-passage mismatch)
#     would very likely close most of this gap; a stronger embedding
#     model might close the rest. Neither is available from
#     `rag/retrieve.py` or `routers/chat.py` alone.
#
# VEC_MAX_DISTANCE is raised from 0.27 to 0.30 -- a modest, defensible
# improvement, NOT a claim of clean separation on the widened set:
#   - Still keeps both mandatory `scripts.smoke` assertions green with
#     margin (A7 grounded worst 0.2194 well under 0.30; A7 refusal best
#     0.3135 stays excluded, 0.0135 of margin -- deliberately short of
#     0.3135 itself, not a knife-edge shave down to 0.313).
#   - Recovers several of the widened set's real paraphrases that 0.27
#     wrongly refused (e.g. "What can I claim for client travel?" 0.2770,
#     "Will the company pay for my certification exam?" 0.2904).
#   - Does NOT and cannot fix PRD §14's own demo phrasing for the
#     carry-forward question (0.3195) -- see the impossibility above.
# Overridable via this module constant; re-measure with the query in this
# file's history before moving it again, and do not chase the demo line
# past 0.3135 -- that reopens A7's seeded refusal.
VEC_MAX_DISTANCE = 0.30
# A couple of exchanges is enough for a rewrite model to resolve pronouns /
# ellipsis without paying for the whole (already-capped) 6-turn history.
REWRITE_HISTORY_TURNS = 4


class _RewriteOutput(BaseModel):
    standalone_query: str


_REWRITE_SYSTEM_PROMPT = (
    "You rewrite a user's follow-up HR-policy question into a standalone "
    "question that makes sense with no prior context. Preserve the original "
    "intent and any policy terms/entities from earlier turns. Respond with "
    "only the rewritten question -- no preamble, no quotes."
)


async def rewrite(question: str, history: list[Any]) -> str:
    """Turn a follow-up into a standalone query.

    Skipped entirely (zero LLM calls) when `history` is empty, per PRD §10.
    When history exists, this tries one cheap LLM call and falls back to a
    local heuristic (last user turn + the question) if that call fails for
    any reason -- a missing/renamed mock fixture, a provider hiccup, a
    validation error. Query rewriting is a quality nicety; it must never be
    a hard dependency for retrieval to run.
    """
    if not history:
        return question

    try:
        agent = get_agent(
            name="chat_rewrite",
            output_type=_RewriteOutput,
            system_prompt=_REWRITE_SYSTEM_PROMPT,
        )
        recent = history[-REWRITE_HISTORY_TURNS:]
        convo = "\n".join(f"{_turn_role(t)}: {_turn_content(t)}" for t in recent)
        result = await agent.run(
            f"Conversation so far:\n{convo}\n\nFollow-up question: {question}"
        )
        rewritten = (result.output.standalone_query or "").strip()
        return rewritten or question
    except Exception:
        logger.info("query rewrite unavailable/failed; using heuristic fallback", exc_info=True)
        last_user = next(
            (_turn_content(t) for t in reversed(history) if _turn_role(t) == "user"), ""
        )
        return f"{last_user} {question}".strip() if last_user else question


def _turn_role(turn: Any) -> str:
    return turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", "user")


def _turn_content(turn: Any) -> str:
    return turn.get("content") if isinstance(turn, dict) else getattr(turn, "content", "")


# ---------------------------------------------------------------------------
# Vector + FTS legs
# ---------------------------------------------------------------------------

_SELECT_COLUMNS = """
    c.id AS chunk_id, c.document_id, c.section, c.text,
    c.start_char, c.end_char, d.title AS document_title
"""


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _vector_search(session: AsyncSession, query: str) -> list[dict[str, Any]]:
    if not _EMBED_AVAILABLE or not query.strip():
        return []
    try:
        qvec = await _maybe_await(embed_query(query))
    except Exception:
        logger.warning("embed_query(%r) failed; skipping vector leg of retrieval", query, exc_info=True)
        return []
    if not qvec:
        return []
    try:
        return await fetch_all(
            session,
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE (c.embedding <=> :qvec) < :max_dist
            ORDER BY c.embedding <=> :qvec
            LIMIT :limit
            """,
            {"qvec": list(qvec), "max_dist": VEC_MAX_DISTANCE, "limit": VEC_LIMIT},
        )
    except Exception:
        logger.warning("pgvector search failed; skipping vector leg of retrieval", exc_info=True)
        return []


async def _fts_search(session: AsyncSession, query: str) -> list[dict[str, Any]]:
    """FTS leg already has a relevance floor and needs no extra one.

    `plainto_tsquery` ANDs together the lexemes of the whole query, and the
    `tsv @@ plainto_tsquery(...)` predicate below only matches rows that
    contain (a stemmed form of) every one of those lexemes -- there is no
    "return the least-bad row anyway" behavior here the way there is with
    nearest-neighbour vector search. Verified empirically against all 12
    `seed/questions.json` questions: both out-of-corpus questions return
    zero FTS rows (neither "contractor" nor "reloc*" appears anywhere in
    the corpus, so the AND can never be satisfied), while several in-corpus
    questions also return zero or one FTS row purely because natural-
    language questions rarely share every stemmed word with the answering
    clause -- retrieval leans on the vector leg for those, which is exactly
    what the hybrid design (PRD §4/§10) is for. No additional floor added.
    """
    if not query.strip():
        return []
    return await fetch_all(
        session,
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.tsv @@ plainto_tsquery('english', :q)
        ORDER BY ts_rank_cd(c.tsv, plainto_tsquery('english', :q)) DESC
        LIMIT :limit
        """,
        {"q": query, "limit": FTS_LIMIT},
    )


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def _rrf_fuse(result_lists: list[list[dict[str, Any]]], *, k: int = RRF_K) -> list[dict[str, Any]]:
    scores: dict[Any, float] = {}
    rows_by_id: dict[Any, dict[str, Any]] = {}
    for rows in result_lists:
        for rank, row in enumerate(rows, start=1):
            cid = row["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            rows_by_id.setdefault(cid, row)
    return sorted(rows_by_id.values(), key=lambda r: scores[r["chunk_id"]], reverse=True)


# ---------------------------------------------------------------------------
# LLM rerank (flag-gated, off by default)
# ---------------------------------------------------------------------------


class _RerankOutput(BaseModel):
    ordered_chunk_ids: list[str]


_RERANK_SYSTEM_PROMPT = (
    "You rank policy chunks by relevance to a query. Given a query and a "
    "numbered list of chunk ids with a text preview, respond with the chunk "
    "ids ordered from most to least relevant. Only use ids you were given."
)


async def _rerank(query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM rerank, gated by `settings.rag_rerank`. Defaults to `False` and
    must stay that way (PRD §10) -- it costs an extra request per question
    for a marginal recall gain on top of RRF. Implemented so a future
    intentional flip of the flag works, never enabled here."""
    if not settings.rag_rerank or len(chunks) <= 1:
        return chunks
    try:
        agent = get_agent(
            name="chat_rerank",
            output_type=_RerankOutput,
            system_prompt=_RERANK_SYSTEM_PROMPT,
        )
        listing = "\n".join(f"{c['chunk_id']}: {c['text'][:200]}" for c in chunks)
        result = await agent.run(f"Query: {query}\n\nChunks:\n{listing}")
        order = {cid: i for i, cid in enumerate(result.output.ordered_chunk_ids)}
        return sorted(chunks, key=lambda c: order.get(str(c["chunk_id"]), len(order)))
    except Exception:
        logger.info("rerank unavailable/failed; keeping RRF order", exc_info=True)
        return chunks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def retrieve(
    session: AsyncSession,
    question: str,
    history: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Full hybrid retrieval pipeline.

    Returns `(standalone_query, chunks)` where `chunks` is at most
    `FINAL_TOP_N` dicts, each with `chunk_id` / `document_id` /
    `document_title` / `section` / `text` / `start_char` / `end_char`. An
    empty list means "nothing to ground an answer on" -- `routers/chat.py`
    treats that as an immediate, free refusal (no LLM call spent).
    """
    standalone = await rewrite(question, history or [])

    vec_rows = await _vector_search(session, standalone)
    fts_rows = await _fts_search(session, standalone)

    fused = _rrf_fuse([vec_rows, fts_rows])[:FUSED_TOP_N]
    final = await _rerank(standalone, fused)
    return standalone, final[:FINAL_TOP_N]
