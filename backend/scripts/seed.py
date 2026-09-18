"""Idempotent seed loader — PRD §10 ("seed data is pre-evaluated and
committed"), §13 (10-16), §14.

Run inside the backend container:

    docker compose exec -T backend python -m scripts.seed

Safe to run repeatedly: every table is loaded via a delete-by-natural-key
(documents/chunks, keyed on `source_name`) or an `INSERT ... ON CONFLICT
(id) DO UPDATE` (jobs, resumes, evaluations, the evaluations' audit_log
rows) against the deterministic ids in `scripts/__init__.py`, so a second
run updates rows in place instead of duplicating them.

Ingestion tiering for the policy corpus (PRD assignment to A7):
  1. `app.rag.ingest`, if importable — A3's real chunk+embed+insert
     pipeline. Best effort: any failure falls back to tier 2 rather than
     aborting the whole seed run.
  2. `app.rag.embed`, if importable — this script does its own light
     section-aware chunking, then calls A3's embedding function directly.
  3. FastEmbed (`BAAI/bge-small-en-v1.5`) called directly. This is *not*
     an LLM call — it is the exact local embedding model the PRD already
     specifies as the default (§5, §7), baked into the backend image at
     build time — so this is a legitimate final fallback, not a shortcut
     around the "never call a model to generate seed data" rule.

Redaction tiering for resumes:
  1. `app.redact`, if importable — A5's real PII-stripping module.
  2. A small inline fallback that removes each seeded candidate's known
     name/email/phone/address (see `scripts.SEED_RESUME_MANIFEST`) plus a
     generic email/phone regex sweep.

Whichever tiers actually ran are printed in the summary at the end, so
the integrator can see at a glance what's stubbed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import execute, fetch_all, session_scope
from scripts import (
    EVALUATIONS_PATH,
    JOBS_PATH,
    POLICIES_DIR,
    QUESTIONS_PATH,
    RESUMES_DIR,
    SEED_DOCUMENT_MANIFEST,
    SEED_EVALUATION_AUDIT_IDS,
    SEED_RESUME_MANIFEST,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:seed:%(message)s")
logger = logging.getLogger("scripts.seed")

_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.*)$", re.MULTILINE)
_SECTION_SPLIT_RE = re.compile(r"^##[ \t]+(.*)$")


# ---------------------------------------------------------------------------
# Policy ingestion — tier 1: app.rag.ingest
# ---------------------------------------------------------------------------


def _find_callable(module: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _extract_chunk_count(result: Any) -> int | None:
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        for key in ("chunk_count", "chunks", "num_chunks"):
            value = result.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, list):
                return len(value)
    for attr in ("chunk_count", "chunks"):
        value = getattr(result, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return None


async def _try_real_ingest(session: AsyncSession, *, title: str, kind: str, source_name: str, text: str) -> int | None:
    """Best-effort call into A3's `app.rag.ingest`. Returns a chunk count
    on success, or None if the module isn't there yet / doesn't expose a
    recognizable entry point / raises for any reason."""
    try:
        from app.rag import ingest as ingest_module
    except ImportError:
        return None

    ingest_fn = _find_callable(ingest_module, ("ingest_document", "ingest_markdown", "ingest_text", "ingest"))
    if ingest_fn is None:
        logger.warning("app.rag.ingest is importable but exposes no recognizable ingest function; using fallback")
        return None

    try:
        sig = inspect.signature(ingest_fn)
        kwargs: dict[str, Any] = {}
        for param_name, value in (
            ("title", title),
            ("kind", kind),
            ("source_name", source_name),
            ("text", text),
            ("content", text),
            ("raw_text", text),
            ("markdown", text),
        ):
            if param_name in sig.parameters and param_name not in kwargs:
                kwargs[param_name] = value
        if "session" in sig.parameters:
            kwargs["session"] = session
        result = ingest_fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        chunk_count = _extract_chunk_count(result)
        if chunk_count is None:
            logger.warning("app.rag.ingest ran but returned an unrecognized shape (%r); using fallback", type(result))
            return None
        return chunk_count
    except Exception as exc:  # noqa: BLE001 - best-effort, never abort the seed run
        logger.warning("app.rag.ingest present but failed (%s); falling back to direct chunk+embed", exc)
        return None


# ---------------------------------------------------------------------------
# Policy ingestion — tier 2/3: local chunking + app.rag.embed or FastEmbed
# ---------------------------------------------------------------------------


def _chunk_markdown(full_text: str) -> list[dict[str, Any]]:
    """Section-aware chunker: one chunk per `##`/`###` heading block, with
    the heading text carried in `section` and prefixed into the chunk body
    (PRD §10 chunking spec) so retrieval sees the section context. Skips
    the document's own H1 title and any heading whose body is empty (a
    parent `##` with no text before its first `###` child)."""
    matches = list(_HEADING_RE.finditer(full_text))
    chunks: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        if level == 1:
            continue
        section_title = m.group(2).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        body = full_text[start:end].rstrip()
        content_after_heading = body[m.end() - m.start() :].strip()
        if len(content_after_heading) < 10:
            continue
        chunks.append(
            {
                "section": section_title,
                "text": body,
                "start_char": start,
                "end_char": start + len(body),
                "token_count": max(1, len(body.split())),
            }
        )
    return chunks


async def _embed_texts(texts: list[str]) -> tuple[list[list[float] | None], str]:
    """Tier 2: `app.rag.embed`. Tier 3: FastEmbed directly (local ONNX,
    no network, no LLM — the PRD's own default embedding provider)."""
    try:
        from app.rag import embed as embed_module

        embed_fn = _find_callable(embed_module, ("embed_texts", "embed_batch", "embed", "get_embeddings"))
        if embed_fn is not None:
            result = embed_fn(texts)
            if inspect.isawaitable(result):
                result = await result
            vectors = [list(map(float, v)) for v in result]
            if len(vectors) == len(texts):
                return vectors, "app.rag.embed"
            logger.warning("app.rag.embed returned %d vectors for %d texts; falling back to FastEmbed", len(vectors), len(texts))
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("app.rag.embed present but failed (%s); falling back to FastEmbed directly", exc)

    try:
        from fastembed import TextEmbedding

        cache_dir = os.environ.get("FASTEMBED_CACHE_PATH")
        model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", cache_dir=cache_dir)
        vectors = [list(map(float, v)) for v in model.embed(texts)]
        return vectors, "fastembed-direct"
    except Exception as exc:  # noqa: BLE001 - embeddings are best-effort for a seed script
        logger.warning("FastEmbed direct embedding failed (%s); storing chunks with NULL embeddings", exc)
        return [None] * len(texts), "none"


async def _fallback_ingest(session: AsyncSession, *, title: str, kind: str, source_name: str, text: str) -> tuple[int, str]:
    chunks = _chunk_markdown(text)
    embeddings, embed_tier = await _embed_texts([c["text"] for c in chunks])

    doc_id = str(uuid.uuid4())
    await execute(
        session,
        """
        INSERT INTO documents (id, title, kind, source_name, char_count)
        VALUES (:id, :title, :kind, :source_name, :char_count)
        """,
        {"id": doc_id, "title": title, "kind": kind, "source_name": source_name, "char_count": len(text)},
    )

    rows = [
        {
            "id": str(uuid.uuid4()),
            "document_id": doc_id,
            "section": c["section"],
            "ordinal": i,
            "text": c["text"],
            "start_char": c["start_char"],
            "end_char": c["end_char"],
            "token_count": c["token_count"],
            "embedding": emb,
        }
        for i, (c, emb) in enumerate(zip(chunks, embeddings))
    ]
    if rows:
        await execute(
            session,
            """
            INSERT INTO chunks (id, document_id, section, ordinal, text, start_char, end_char, token_count, embedding)
            VALUES (:id, :document_id, :section, :ordinal, :text, :start_char, :end_char, :token_count, :embedding)
            """,
            rows,
        )
    return len(rows), f"fallback-chunk+{embed_tier}"


async def ingest_policy_document(session: AsyncSession, *, title: str, kind: str, source_name: str, text: str) -> tuple[int, str]:
    """Idempotent: always deletes any existing document with this
    `source_name` first (cascades to its chunks), regardless of which
    tier actually re-inserts it, so a second `scripts.seed` run never
    duplicates a policy document."""
    await execute(session, "DELETE FROM documents WHERE source_name = :source_name", {"source_name": source_name})

    chunk_count = await _try_real_ingest(session, title=title, kind=kind, source_name=source_name, text=text)
    if chunk_count is not None:
        return chunk_count, "app.rag.ingest"

    return await _fallback_ingest(session, title=title, kind=kind, source_name=source_name, text=text)


# ---------------------------------------------------------------------------
# Resume redaction
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\-\s]{7,14}\d")


def _fallback_redact(raw_text: str, meta: dict[str, str]) -> str:
    text = raw_text
    for field, label in (
        ("name", "[REDACTED NAME]"),
        ("email", "[REDACTED EMAIL]"),
        ("phone", "[REDACTED PHONE]"),
        ("address", "[REDACTED ADDRESS]"),
    ):
        value = meta.get(field)
        if value:
            text = text.replace(value, label)
    text = _EMAIL_RE.sub("[REDACTED EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED PHONE]", text)
    return text


async def _apply_redaction(raw_text: str, meta: dict[str, str]) -> tuple[str, str]:
    try:
        from app import redact as redact_module

        redact_fn = _find_callable(redact_module, ("redact_text", "redact_resume", "redact", "redact_resume_text"))
        if redact_fn is not None:
            result = redact_fn(raw_text)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str) and result.strip():
                return result, "app.redact"
            logger.warning("app.redact present but returned an unusable result; using fallback for %s", meta["file_name"])
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("app.redact present but failed (%s); using fallback for %s", exc, meta["file_name"])

    return _fallback_redact(raw_text, meta), "fallback"


def _split_sections(raw_text: str) -> dict[str, str]:
    """Splits on `## Heading` lines only (not `###`), so resume
    subsections like `### Senior Backend Engineer, ...` stay nested inside
    their parent `## Experience` section body -- matching the
    `get_resume_section(resume_id, section)` tool's expected granularity."""
    sections: dict[str, str] = {}
    current_key = "header"
    current_lines: list[str] = []
    for line in raw_text.splitlines():
        m = _SECTION_SPLIT_RE.match(line)
        if m:
            if current_lines:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = m.group(1).strip().lower()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


# ---------------------------------------------------------------------------
# Upserts
# ---------------------------------------------------------------------------


async def upsert_job(session: AsyncSession, job: dict[str, Any]) -> None:
    await execute(
        session,
        """
        INSERT INTO jobs (id, title, level, location, department, description_md,
                           must_haves, nice_to_haves, min_years, comp_min, comp_max, rubric_weights)
        VALUES (:id, :title, :level, :location, :department, :description_md,
                :must_haves, :nice_to_haves, :min_years, :comp_min, :comp_max, :rubric_weights)
        ON CONFLICT (id) DO UPDATE SET
          title = EXCLUDED.title, level = EXCLUDED.level, location = EXCLUDED.location,
          department = EXCLUDED.department, description_md = EXCLUDED.description_md,
          must_haves = EXCLUDED.must_haves, nice_to_haves = EXCLUDED.nice_to_haves,
          min_years = EXCLUDED.min_years, comp_min = EXCLUDED.comp_min, comp_max = EXCLUDED.comp_max,
          rubric_weights = EXCLUDED.rubric_weights
        """,
        {
            "id": job["id"],
            "title": job["title"],
            "level": job.get("level"),
            "location": job.get("location"),
            "department": job.get("department"),
            "description_md": job.get("description_md"),
            "must_haves": job.get("must_haves", []),
            "nice_to_haves": job.get("nice_to_haves", []),
            "min_years": job.get("min_years"),
            "comp_min": job.get("comp_min"),
            "comp_max": job.get("comp_max"),
            "rubric_weights": job.get("rubric_weights"),
        },
    )


async def upsert_resume(session: AsyncSession, meta: dict[str, str], raw_text: str, redacted_text: str, sections: dict[str, str]) -> None:
    await execute(
        session,
        """
        INSERT INTO resumes (id, candidate_label, file_name, raw_text, redacted_text, sections, status, error)
        VALUES (:id, :candidate_label, :file_name, :raw_text, :redacted_text, :sections, 'scored', NULL)
        ON CONFLICT (id) DO UPDATE SET
          candidate_label = EXCLUDED.candidate_label, file_name = EXCLUDED.file_name,
          raw_text = EXCLUDED.raw_text, redacted_text = EXCLUDED.redacted_text,
          sections = EXCLUDED.sections, status = EXCLUDED.status, error = NULL
        """,
        {
            "id": meta["id"],
            "candidate_label": meta["candidate_label"],
            "file_name": meta["file_name"],
            "raw_text": raw_text,
            "redacted_text": redacted_text,
            "sections": sections,
        },
    )


async def upsert_evaluation(session: AsyncSession, ev: dict[str, Any]) -> None:
    meta = ev["meta"]
    await execute(
        session,
        """
        INSERT INTO evaluations (id, resume_id, job_id, fit_score, recommendation, criteria,
                                  matched_skills, gaps, summary, provider, model,
                                  rubric_version, prompt_version, input_tokens, output_tokens, latency_ms)
        VALUES (:id, :resume_id, :job_id, :fit_score, :recommendation, :criteria,
                :matched_skills, :gaps, :summary, :provider, :model,
                :rubric_version, :prompt_version, :input_tokens, :output_tokens, :latency_ms)
        ON CONFLICT (id) DO UPDATE SET
          fit_score = EXCLUDED.fit_score, recommendation = EXCLUDED.recommendation, criteria = EXCLUDED.criteria,
          matched_skills = EXCLUDED.matched_skills, gaps = EXCLUDED.gaps, summary = EXCLUDED.summary,
          provider = EXCLUDED.provider, model = EXCLUDED.model, rubric_version = EXCLUDED.rubric_version,
          prompt_version = EXCLUDED.prompt_version, input_tokens = EXCLUDED.input_tokens,
          output_tokens = EXCLUDED.output_tokens, latency_ms = EXCLUDED.latency_ms
        """,
        {
            "id": ev["id"],
            "resume_id": ev["resume_id"],
            "job_id": ev["job_id"],
            "fit_score": ev["fit_score"],
            "recommendation": ev["recommendation"],
            "criteria": ev["criteria"],
            "matched_skills": ev.get("matched_skills", []),
            "gaps": ev.get("gaps", []),
            "summary": ev.get("summary"),
            "provider": meta["provider"],
            "model": meta["model"],
            "rubric_version": meta["rubric_version"],
            "prompt_version": meta["prompt_version"],
            "input_tokens": meta.get("input_tokens", 0),
            "output_tokens": meta.get("output_tokens", 0),
            "latency_ms": meta.get("latency_ms", 0),
        },
    )


async def upsert_evaluation_audit_log(session: AsyncSession, ev: dict[str, Any]) -> None:
    """Every evaluation must have an immutable `audit_log` row recording
    provider/model/rubric version and a tool trace (PRD §4 F2, §14 DoD).
    Uses `action='evaluate'`, never `'llm_call'` -- the daily LLM budget
    counter (`app.llm.throttle.budget_used_today`) only scans
    `action='llm_call'` rows, so these pre-computed seed evaluations never
    touch the free-tier request budget, no matter what `provider` string
    they carry."""
    audit_id = SEED_EVALUATION_AUDIT_IDS[ev["id"]]
    meta = ev["meta"]
    tool_trace = [
        {"tool": "get_jd", "args": {"job_id": ev["job_id"]}},
        {"tool": "get_resume_section", "args": {"resume_id": ev["resume_id"], "section": "experience"}},
        {"tool": "get_resume_section", "args": {"resume_id": ev["resume_id"], "section": "education"}},
    ]
    payload = {
        "provider": meta["provider"],
        "model": meta["model"],
        "rubric_version": meta["rubric_version"],
        "prompt_version": meta["prompt_version"],
        "fit_score": ev["fit_score"],
        "recommendation": ev["recommendation"],
        "seed": True,
    }
    await execute(
        session,
        """
        INSERT INTO audit_log (id, entity_type, entity_id, action, actor, tool_trace, payload)
        VALUES (:id, 'evaluation', :entity_id, 'evaluate', 'seed', :tool_trace, :payload)
        ON CONFLICT (id) DO UPDATE SET tool_trace = EXCLUDED.tool_trace, payload = EXCLUDED.payload
        """,
        {"id": audit_id, "entity_id": ev["id"], "tool_trace": tool_trace, "payload": payload},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    report: dict[str, Any] = {
        "documents": 0,
        "chunks": 0,
        "jobs": 0,
        "resumes": 0,
        "evaluations": 0,
        "ingest_tiers_used": set(),
        "redact_tiers_used": set(),
    }

    async with session_scope() as session:
        for doc in SEED_DOCUMENT_MANIFEST:
            path = POLICIES_DIR / doc["source_name"]
            text = path.read_text(encoding="utf-8")
            chunk_count, tier = await ingest_policy_document(
                session, title=doc["title"], kind=doc["kind"], source_name=doc["source_name"], text=text
            )
            report["documents"] += 1
            report["chunks"] += chunk_count
            report["ingest_tiers_used"].add(tier)
            logger.info("ingested %s -> %d chunks (%s)", doc["source_name"], chunk_count, tier)

        jobs = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        for job in jobs:
            await upsert_job(session, job)
            report["jobs"] += 1

        for meta in SEED_RESUME_MANIFEST:
            path = RESUMES_DIR / meta["file_name"]
            raw_text = path.read_text(encoding="utf-8")
            redacted_text, tier = await _apply_redaction(raw_text, meta)
            sections = _split_sections(raw_text)
            await upsert_resume(session, meta, raw_text, redacted_text, sections)
            report["resumes"] += 1
            report["redact_tiers_used"].add(tier)

        evaluations = json.loads(EVALUATIONS_PATH.read_text(encoding="utf-8"))
        for ev in evaluations:
            await upsert_evaluation(session, ev)
            await upsert_evaluation_audit_log(session, ev)
            report["evaluations"] += 1

        # Sanity check: questions.json must at least be valid JSON with the
        # expected shape -- it's read live by scripts.smoke, not loaded
        # into any table, but a malformed file should fail loudly here
        # rather than surface as a confusing smoke-test crash later.
        questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
        grounded = sum(1 for q in questions if q.get("type") == "grounded")
        refusal = sum(1 for q in questions if q.get("type") == "refusal")
        report["questions"] = {"grounded": grounded, "refusal": refusal}

    row_counts = await _row_counts()

    report["ingest_tiers_used"] = sorted(report["ingest_tiers_used"])
    report["redact_tiers_used"] = sorted(report["redact_tiers_used"])
    report["db_row_counts"] = row_counts
    print(json.dumps(report, indent=2, default=str))


async def _row_counts() -> dict[str, int]:
    async with session_scope() as session:
        rows = await fetch_all(
            session,
            """
            SELECT
              (SELECT count(*) FROM documents)  AS documents,
              (SELECT count(*) FROM chunks)     AS chunks,
              (SELECT count(*) FROM jobs)       AS jobs,
              (SELECT count(*) FROM resumes)    AS resumes,
              (SELECT count(*) FROM evaluations) AS evaluations,
              (SELECT count(*) FROM audit_log)  AS audit_log
            """,
        )
    return dict(rows[0]) if rows else {}


if __name__ == "__main__":
    asyncio.run(main())
