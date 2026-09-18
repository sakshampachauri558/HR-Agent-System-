"""F2 — the agentic resume evaluator. PRD §4 F2, §10 "Evaluator agent".

A Pydantic AI agent (via `app.llm.provider.get_agent`) scores a redacted
resume against a job's rubric. Scoring is NOT a tool — all seven
`DEFAULT_RUBRIC` criteria come back in one typed `EvaluationResult`. Tools
are reserved for *fetching* context the model can't already see:

- `search_policy(query)`  — reuses F1 retrieval (`app.rag.retrieve`), import
  guarded since A4 builds that module in parallel.
- `get_jd(job_id)`        — the parsed job description.
- `get_resume_section(resume_id, section)` — one resume section at a time.

The redacted resume text is the user message; the JD + rubric + guardrails
go in the system prompt (PRD §10). Hard cap: 6 tool calls total, enforced
by having each tool return a "stop calling tools, finalize now" message
past the cap rather than doing real work.

After the model returns, this module (not the model) is the source of
truth for:
- `fit_score` / `recommendation` — recomputed deterministically as a
  weighted sum over the job's rubric, never trusted from the LLM's own
  arithmetic.
- Evidence-quote verification — every non-null `evidence_quote` must be a
  substring of `resumes.redacted_text` (whitespace-normalized). A quote
  that fails is flagged inline in that criterion's `reasoning` (the frozen
  `CriterionScore` schema has no dedicated `verified` field to carry a
  flag in) and recorded in the `audit_log.payload.verification` list so a
  future UI can query it structurally instead of string-matching.
- Missing criteria (e.g. the model stopped short of all 7, or hit the tool
  cap) are padded with `score=0, evidence_quote=None,
  reasoning="not evidenced"` so every evaluation always reports all 7
  rubric rows.

Every evaluation writes one `audit_log` row (`entity_type="evaluation"`)
with provider, model, `RUBRIC_VERSION`, prompt version, and the full tool
trace — in addition to the generic `llm_call` audit row `app.llm.provider`
already writes for budget/usage accounting.
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any
from uuid import UUID, uuid4

from app import redact
from app.config import settings
from app.db import execute, fetch_one, session_scope
from app.llm.mock import MOCK_MODEL_NAME
from app.llm.provider import PROVIDERS, get_agent
from app.schemas import (
    DEFAULT_RUBRIC,
    RUBRIC_VERSION,
    CriterionScore,
    EvalMeta,
    EvaluationResult,
    RubricCriterion,
)

PROMPT_VERSION = "v1"
MAX_TOOL_CALLS = 6


# ---------------------------------------------------------------------------
# Row <-> model conversion
# ---------------------------------------------------------------------------


def row_to_evaluation_result(row: dict[str, Any]) -> EvaluationResult:
    """Convert an `evaluations` table row (JSONB columns already decoded to
    python by the codec in `app.db`) into the frozen `EvaluationResult`."""
    return EvaluationResult(
        id=row["id"],
        resume_id=row["resume_id"],
        job_id=row["job_id"],
        fit_score=row["fit_score"] or 0,
        recommendation=row["recommendation"] or "no",
        criteria=[CriterionScore(**c) for c in (row.get("criteria") or [])],
        matched_skills=row.get("matched_skills") or [],
        gaps=row.get("gaps") or [],
        summary=row.get("summary") or "",
        meta=EvalMeta(
            provider=row.get("provider") or "unknown",
            model=row.get("model") or "unknown",
            rubric_version=row.get("rubric_version") or RUBRIC_VERSION,
            prompt_version=row.get("prompt_version") or PROMPT_VERSION,
            input_tokens=row.get("input_tokens") or 0,
            output_tokens=row.get("output_tokens") or 0,
            latency_ms=row.get("latency_ms") or 0,
        ),
    )


# ---------------------------------------------------------------------------
# Rubric helpers
# ---------------------------------------------------------------------------


def _rubric_for_job(job: dict[str, Any]) -> list[RubricCriterion]:
    raw = job.get("rubric_weights")
    if not raw:
        return list(DEFAULT_RUBRIC)
    return [rc if isinstance(rc, RubricCriterion) else RubricCriterion(**rc) for rc in raw]


def _ensure_all_criteria(
    criteria: list[CriterionScore], rubric: list[RubricCriterion]
) -> list[CriterionScore]:
    have = {c.criterion.strip().lower() for c in criteria}
    missing = [rc for rc in rubric if rc.criterion.strip().lower() not in have]
    padded = list(criteria)
    for rc in missing:
        padded.append(
            CriterionScore(
                criterion=rc.criterion,
                score=0,
                weight=rc.weight,
                evidence_quote=None,
                reasoning="not evidenced",
            )
        )
    return padded


def _apply_canonical_weights(criteria: list[CriterionScore], rubric: list[RubricCriterion]) -> None:
    weight_by_name = {rc.criterion.strip().lower(): rc.weight for rc in rubric}
    for c in criteria:
        canonical = weight_by_name.get(c.criterion.strip().lower())
        if canonical is not None:
            c.weight = canonical


def _recompute_fit(criteria: list[CriterionScore]) -> tuple[int, str]:
    total_weight = sum(c.weight for c in criteria) or 1.0
    weighted = sum(c.score * c.weight for c in criteria)
    fit = round((weighted / total_weight) * 10)
    fit = max(0, min(100, fit))
    if fit >= 85:
        recommendation = "strong_yes"
    elif fit >= 70:
        recommendation = "yes"
    elif fit >= 50:
        recommendation = "maybe"
    else:
        recommendation = "no"
    return fit, recommendation


def _verify_criteria(criteria: list[CriterionScore], redacted_text: str) -> list[dict[str, Any]]:
    """The cheap, deterministic hallucination check (PRD §10): every
    non-null evidence_quote must be a whitespace-normalized substring of
    the redacted resume text. Failures get an inline flag in `reasoning`
    (frozen schema has no dedicated `verified` field) and are also
    reported structurally in the returned list for `audit_log`."""
    norm_source = redact.normalize_whitespace(redacted_text)
    results: list[dict[str, Any]] = []
    for c in criteria:
        if c.evidence_quote is None:
            results.append({"criterion": c.criterion, "verified": None})
            continue
        norm_quote = redact.normalize_whitespace(c.evidence_quote)
        verified = bool(norm_quote) and norm_quote in norm_source
        if not verified:
            note = " [unverified: quote not found in redacted resume text]"
            budget = max(0, 300 - len(note))
            c.reasoning = (c.reasoning or "")[:budget] + note
        results.append({"criterion": c.criterion, "verified": verified, "quote": c.evidence_quote})
    return results


# ---------------------------------------------------------------------------
# search_policy formatting helpers
# ---------------------------------------------------------------------------


def _format_policy_result(result: Any) -> str:
    if not result:
        return "No matching internal policy found."
    if isinstance(result, str):
        return result
    items = result if isinstance(result, list) else (result.get("chunks") or result.get("results") or [])
    lines: list[str] = []
    for item in items[:3]:
        if isinstance(item, dict):
            title = item.get("document_title") or item.get("title") or "Policy"
            section = item.get("section") or ""
            text = item.get("text") or item.get("quote") or ""
            lines.append(f"[{title} - {section}] {text}".strip())
        else:
            lines.append(str(item))
    return "\n".join(lines) if lines else "No matching internal policy found."


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------


async def evaluate_resume(resume_id: UUID, job_id: UUID) -> EvaluationResult:
    async with session_scope() as session:
        resume = await fetch_one(session, "SELECT * FROM resumes WHERE id = :id", {"id": resume_id})
        job = await fetch_one(session, "SELECT * FROM jobs WHERE id = :id", {"id": job_id})

    if resume is None:
        raise ValueError(f"resume {resume_id} not found")
    if job is None:
        raise ValueError(f"job {job_id} not found")

    redacted_text = resume.get("redacted_text") or ""
    if not redacted_text.strip():
        raise ValueError(f"resume {resume_id} has no redacted_text yet (upload/parse incomplete)")

    sections = resume.get("sections") or {}
    rubric = _rubric_for_job(job)

    tool_trace: list[dict[str, Any]] = []
    call_count = {"n": 0}

    def _record(tool_name: str, args: dict[str, Any], result_preview: str) -> None:
        call_count["n"] += 1
        tool_trace.append(
            {
                "tool": tool_name,
                "args": args,
                "call_number": call_count["n"],
                "result_preview": (result_preview or "")[:200],
            }
        )

    def _cap_message() -> str:
        return (
            f"Tool call budget exhausted ({MAX_TOOL_CALLS} calls). "
            "Stop calling tools and finalize your EvaluationResult now using "
            "whatever information you already have; mark any still-unscored "
            "criterion low with evidence_quote=null and reasoning='not evidenced'."
        )

    async def search_policy(query: str) -> str:
        """Check internal hiring-bar / role-level policy relevant to `query`."""
        if call_count["n"] >= MAX_TOOL_CALLS:
            return _cap_message()
        try:
            # A4 owns app.rag.retrieve and builds it in parallel; it may not exist yet.
            from app.rag import retrieve as retrieve_mod
        except ImportError:
            text_out = "Internal policy search is not available in this build; proceed without it."
            _record("search_policy", {"query": query}, text_out)
            return text_out

        fn = (
            getattr(retrieve_mod, "retrieve", None)
            or getattr(retrieve_mod, "search", None)
            or getattr(retrieve_mod, "hybrid_search", None)
        )
        if fn is None:
            text_out = "Internal policy search is not available in this build; proceed without it."
            _record("search_policy", {"query": query}, text_out)
            return text_out

        try:
            result = fn(query)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - a broken retriever must never break evaluation
            text_out = f"Policy search failed ({exc}); proceed without it."
            _record("search_policy", {"query": query}, text_out)
            return text_out

        text_out = _format_policy_result(result)
        _record("search_policy", {"query": query}, text_out)
        return text_out

    async def get_jd(job_id_arg: str) -> str:
        """Fetch the parsed job description: must-haves, nice-to-haves, level, min years."""
        if call_count["n"] >= MAX_TOOL_CALLS:
            return _cap_message()
        text_out = json.dumps(
            {
                "title": job.get("title"),
                "level": job.get("level"),
                "department": job.get("department"),
                "location": job.get("location"),
                "min_years": job.get("min_years"),
                "must_haves": job.get("must_haves") or [],
                "nice_to_haves": job.get("nice_to_haves") or [],
            }
        )
        _record("get_jd", {"job_id": job_id_arg}, text_out)
        return text_out

    async def get_resume_section(resume_id_arg: str, section: str) -> str:
        """Pull one resume section (experience/education/skills/projects/summary/other)."""
        if call_count["n"] >= MAX_TOOL_CALLS:
            return _cap_message()
        key = (section or "").strip().lower()
        text_out = sections.get(key) or sections.get(section or "") or ""
        if not text_out:
            available = ", ".join(sections.keys()) or "none"
            text_out = f"No content found for section '{section}'. Available sections: {available}."
        _record("get_resume_section", {"resume_id": resume_id_arg, "section": section}, text_out)
        return text_out

    system_prompt = _build_system_prompt(job, rubric)
    user_prompt = _build_user_prompt(resume, redacted_text)

    agent = get_agent(
        name="evaluation",
        output_type=EvaluationResult,
        system_prompt=system_prompt,
        tools=[search_policy, get_jd, get_resume_section],
    )

    new_eval_id = uuid4()
    started = time.monotonic()
    run_result = await agent.run(
        user_prompt,
        deps={"id": new_eval_id, "resume_id": resume_id, "job_id": job_id},
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    output: EvaluationResult = run_result.output
    usage = run_result.usage()
    input_tokens = int(getattr(usage, "request_tokens", None) or getattr(usage, "input_tokens", None) or 0)
    output_tokens = int(getattr(usage, "response_tokens", None) or getattr(usage, "output_tokens", None) or 0)

    output.resume_id = resume_id
    output.job_id = job_id

    criteria = _ensure_all_criteria(output.criteria, rubric)
    _apply_canonical_weights(criteria, rubric)
    verification = _verify_criteria(criteria, redacted_text)
    fit_score, recommendation = _recompute_fit(criteria)

    output.criteria = criteria
    output.fit_score = fit_score
    output.recommendation = recommendation

    # `_ThrottledAgent`/`MockAgent` don't surface which provider actually
    # served the call back to us (a gap in the frozen provider.py
    # contract), so provider/model are read from configured settings
    # rather than trusted from the model's own (unreliable) self-report.
    if settings.llm_provider == "mock":
        provider_used, model_used = "mock", MOCK_MODEL_NAME
    else:
        provider_used = settings.llm_provider
        cfg = PROVIDERS.get(provider_used)
        model_used = cfg.default_model if cfg else "unknown"

    output.meta = EvalMeta(
        provider=provider_used,
        model=model_used,
        rubric_version=RUBRIC_VERSION,
        prompt_version=PROMPT_VERSION,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )

    async with session_scope() as session:
        row = await fetch_one(
            session,
            """
            INSERT INTO evaluations (
                id, resume_id, job_id, fit_score, recommendation, criteria,
                matched_skills, gaps, summary, provider, model, rubric_version,
                prompt_version, input_tokens, output_tokens, latency_ms
            ) VALUES (
                :id, :resume_id, :job_id, :fit_score, :recommendation, :criteria,
                :matched_skills, :gaps, :summary, :provider, :model, :rubric_version,
                :prompt_version, :input_tokens, :output_tokens, :latency_ms
            )
            ON CONFLICT (resume_id, job_id) DO UPDATE SET
                fit_score = EXCLUDED.fit_score,
                recommendation = EXCLUDED.recommendation,
                criteria = EXCLUDED.criteria,
                matched_skills = EXCLUDED.matched_skills,
                gaps = EXCLUDED.gaps,
                summary = EXCLUDED.summary,
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                rubric_version = EXCLUDED.rubric_version,
                prompt_version = EXCLUDED.prompt_version,
                input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                latency_ms = EXCLUDED.latency_ms
            RETURNING id
            """,
            {
                "id": new_eval_id,
                "resume_id": resume_id,
                "job_id": job_id,
                "fit_score": output.fit_score,
                "recommendation": output.recommendation,
                "criteria": [c.model_dump(mode="json") for c in output.criteria],
                "matched_skills": output.matched_skills,
                "gaps": output.gaps,
                "summary": output.summary,
                "provider": provider_used,
                "model": model_used,
                "rubric_version": RUBRIC_VERSION,
                "prompt_version": PROMPT_VERSION,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
            },
        )
        final_id = row["id"]

        await execute(
            session,
            """
            INSERT INTO audit_log (entity_type, entity_id, action, actor, tool_trace, payload)
            VALUES ('evaluation', :entity_id, 'evaluate', 'evaluator_agent', :tool_trace, :payload)
            """,
            {
                "entity_id": final_id,
                "tool_trace": tool_trace,
                "payload": {
                    "provider": provider_used,
                    "model": model_used,
                    "rubric_version": RUBRIC_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "resume_id": str(resume_id),
                    "job_id": str(job_id),
                    "verification": verification,
                },
            },
        )

    output.id = final_id
    return output


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_system_prompt(job: dict[str, Any], rubric: list[RubricCriterion]) -> str:
    rubric_lines = "\n".join(f"- {rc.criterion} (weight {rc.weight:.0%})" for rc in rubric)
    must_haves = ", ".join(job.get("must_haves") or []) or "none listed"
    nice_to_haves = ", ".join(job.get("nice_to_haves") or []) or "none listed"
    min_years = job.get("min_years")
    min_years_text = str(min_years) if min_years is not None else "unspecified"

    return f"""You are an unbiased resume screening evaluator for the role \
"{job.get('title')}" (level: {job.get('level') or 'unspecified'}).

Job requirements:
- Must-have: {must_haves}
- Nice-to-have: {nice_to_haves}
- Minimum years of experience: {min_years_text}

Score the candidate against exactly these rubric criteria, using their given weights:
{rubric_lines}

You will be given the candidate's resume text with personal-identity fields already \
redacted (name, contact info, address, photo references, gender markers, date of \
birth, nationality, marital status; university names reduced to degree + field). \
This redacted text is the ONLY information you may use about the candidate.

Hard guardrails:
- Never infer or reference race, gender, age, nationality, marital status, disability, \
or any other protected attribute, even if a fragment slips through redaction.
- Never score based on candidate name, school/employer prestige, employment-date gaps, \
or any photo reference — score only on demonstrated skills, experience, and impact.
- Every `evidence_quote` you give MUST be copied verbatim, character-for-character, \
from the resume text you were shown (same spacing and punctuation). It will be checked \
programmatically against the source text. Never paraphrase and never invent a quote.
- If a criterion has no supporting text, score it low (0-3), set `evidence_quote` to \
null, and set `reasoning` to "not evidenced". Never fabricate evidence.
- You have three read-only tools (`search_policy`, `get_jd`, `get_resume_section`) for \
supplementary context, but the full redacted resume is already in your prompt — only \
call a tool if you genuinely need more. You have a hard cap of {MAX_TOOL_CALLS} tool \
calls total; once a tool tells you the budget is exhausted, stop calling tools and \
finalize immediately.
- Scoring is not a tool call. Return all {len(rubric)} rubric criteria in a single \
final structured `EvaluationResult` — there is no per-criterion scoring tool.
- Derive `matched_skills` and `gaps` from what is/isn't evidenced in the text. Keep \
`summary` to 2-3 sentences.
"""


def _build_user_prompt(resume: dict[str, Any], redacted_text: str) -> str:
    label = resume.get("candidate_label") or "the candidate"
    return (
        f'Redacted resume for "{label}":\n'
        "---\n"
        f"{redacted_text}\n"
        "---\n\n"
        "Score this candidate against the rubric above using only the text shown."
    )
