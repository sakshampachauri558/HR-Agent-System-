"""Acceptance harness — PRD §10 (verification), §14 (definition of done).

Two modes:

    docker compose exec -T backend python -m scripts.smoke
        Default. LLM_PROVIDER should be "mock" (the committed default).
        Zero LLM requests. Asserts PRD §14's definition of done against
        whatever Wave-1/2 modules have landed so far, skipping (loudly,
        not crashing) any check whose dependency isn't mounted yet.

    docker compose exec -T backend python -m scripts.smoke --probe
        Gate-only. Requires LLM_PROVIDER=openrouter (or another real
        provider) and a configured API key. Spends ~2 real requests to
        assert the provider is reachable and that the configured free
        model actually supports tool calling (PRD §5/§10 — several
        OpenRouter `:free` models silently don't, which is fatal for
        F2's agent). Never run this from a build/dev agent loop.

Exit code is non-zero if any check failed (skips do not fail the run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

import httpx

from app.db import fetch_all, session_scope
from scripts import QUESTIONS_PATH, SEED_JOB_SENIOR_BACKEND_ID, SEED_RESUME_MANIFEST

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(f"{name}" + (f" — {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.failed.append(f"{name} — {detail}")

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append(f"{name} — SKIPPED: {reason}")

    def render(self) -> str:
        lines = ["=" * 72, "PeopleOps Copilot — smoke test report", "=" * 72]
        for p in self.passed:
            lines.append(f"  [PASS] {p}")
        for s in self.skipped:
            lines.append(f"  [SKIP] {s}")
        for f in self.failed:
            lines.append(f"  [FAIL] {f}")
        lines.append("-" * 72)
        lines.append(f"{len(self.passed)} passed, {len(self.skipped)} skipped, {len(self.failed)} failed")
        return "\n".join(lines)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------------
# F1 — policy chat (skips cleanly if app.routers.chat isn't mounted yet)
# ---------------------------------------------------------------------------


async def _citations_resolve(citations: list[dict[str, Any]]) -> bool:
    if not citations:
        return False
    async with session_scope() as session:
        for c in citations:
            chunk_id = c.get("chunk_id")
            if not chunk_id:
                continue
            rows = await fetch_all(session, "SELECT id FROM chunks WHERE id = :id", {"id": str(chunk_id)})
            if rows:
                return True
    return False


async def check_policy_chat(report: Report, client: httpx.AsyncClient | None) -> None:
    questions: list[dict[str, Any]] = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    grounded_qs = [q for q in questions if q.get("type") == "grounded"]
    refusal_qs = [q for q in questions if q.get("type") == "refusal"]

    if client is None:
        report.skip("F1 grounded Q&A", "app.routers.chat not mounted yet (POST /api/chat unavailable)")
        report.skip("F1 refusal path", "app.routers.chat not mounted yet (POST /api/chat unavailable)")
        return

    grounded_pass = 0
    for q in grounded_qs:
        label = q["question"][:60]
        try:
            resp = await client.post("/api/chat", json={"question": q["question"], "history": []}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            report.fail(f"chat request ({label!r})", str(exc))
            continue
        if resp.status_code != 200:
            report.fail(f"chat request ({label!r})", f"HTTP {resp.status_code}: {resp.text[:200]}")
            continue
        data = resp.json()
        citations = data.get("citations") or []
        if data.get("grounded") and citations and await _citations_resolve(citations):
            grounded_pass += 1
        else:
            report.fail(
                f"grounded answer ({label!r})",
                f"grounded={data.get('grounded')}, citations={len(citations)}, resolvable={await _citations_resolve(citations)}",
            )
    if grounded_qs:
        detail = f"{grounded_pass}/{len(grounded_qs)} seeded questions returned a grounded, cited answer"
        (report.ok if grounded_pass == len(grounded_qs) else report.fail)("F1 grounded Q&A", detail)

    refusal_pass = 0
    for q in refusal_qs:
        label = q["question"][:60]
        try:
            resp = await client.post("/api/chat", json={"question": q["question"], "history": []}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            report.fail(f"chat request ({label!r})", str(exc))
            continue
        if resp.status_code != 200:
            report.fail(f"chat request ({label!r})", f"HTTP {resp.status_code}: {resp.text[:200]}")
            continue
        data = resp.json()
        if data.get("grounded") is False and not (data.get("citations") or []):
            refusal_pass += 1
        else:
            report.fail(
                f"refusal path ({label!r})",
                f"expected grounded=false with no citations, got grounded={data.get('grounded')}, citations={len(data.get('citations') or [])}",
            )
    if refusal_qs:
        detail = f"{refusal_pass}/{len(refusal_qs)} out-of-corpus questions refused cleanly"
        (report.ok if refusal_pass == len(refusal_qs) else report.fail)("F1 refusal path", detail)


# ---------------------------------------------------------------------------
# F2 — evaluations, hallucination check, audit log, PII (all DB-level —
# these come from scripts.seed's pre-computed fixtures, per PRD §10's
# "seed data is pre-evaluated and committed", so none of this depends on
# app.routers.evaluate having landed).
# ---------------------------------------------------------------------------


async def check_evaluations(report: Report) -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = await fetch_all(
            session,
            """
            SELECT id, resume_id, job_id, fit_score, recommendation, criteria
            FROM evaluations
            WHERE job_id = :job_id
            """,
            {"job_id": SEED_JOB_SENIOR_BACKEND_ID},
        )
    if len(rows) < 5:
        report.fail("F2 evaluation count", f"expected >=5 evaluations for the Senior Backend Engineer job, found {len(rows)}")
    else:
        report.ok("F2 evaluation count", f"{len(rows)} resumes evaluated against 1 job")

    under_scored = [str(r["id"]) for r in rows if len(r["criteria"] or []) < 6]
    if under_scored:
        report.fail("F2 criteria count", f"evaluations with <6 criteria: {under_scored}")
    elif rows:
        report.ok("F2 criteria count", "every evaluation scores >=6 criteria")
    return rows


async def check_evidence_quotes(report: Report, eval_rows: list[dict[str, Any]]) -> None:
    if not eval_rows:
        report.skip("evidence_quote hallucination check", "no evaluations to check")
        return
    async with session_scope() as session:
        resumes = await fetch_all(session, "SELECT id, redacted_text FROM resumes")
    redacted_by_id = {str(r["id"]): _norm(r["redacted_text"]) for r in resumes}

    checked = 0
    failures = 0
    for row in eval_rows:
        redacted = redacted_by_id.get(str(row["resume_id"]))
        if redacted is None:
            report.fail(f"evidence_quote check (evaluation {row['id']})", "resume not found / has no redacted_text")
            continue
        for c in row["criteria"] or []:
            quote = c.get("evidence_quote")
            if quote is None:
                continue
            checked += 1
            if _norm(quote) not in redacted:
                failures += 1
                report.fail(
                    f"evidence_quote substring ({c.get('criterion')}, evaluation {row['id']})",
                    "quote is not a substring of the matching resume's redacted_text",
                )
    if checked == 0:
        report.skip("evidence_quote hallucination check", "no non-null evidence_quotes found to check")
    elif failures == 0:
        report.ok("evidence_quote hallucination check", f"{checked} non-null evidence quotes verified as substrings of redacted_text")


async def check_audit_log(report: Report, eval_rows: list[dict[str, Any]]) -> None:
    if not eval_rows:
        report.skip("audit_log for evaluations", "no evaluations to check")
        return
    ids = [str(r["id"]) for r in eval_rows]
    async with session_scope() as session:
        rows = await fetch_all(
            session,
            "SELECT entity_id, payload, tool_trace FROM audit_log WHERE entity_type = 'evaluation' AND action = 'evaluate'",
        )
    by_entity = {str(r["entity_id"]): r for r in rows}
    missing = [i for i in ids if i not in by_entity]
    if missing:
        report.fail("audit_log for evaluations", f"missing audit_log row for {len(missing)}/{len(ids)} evaluations")
        return

    incomplete = []
    for i in ids:
        payload = by_entity[i]["payload"] or {}
        has_fields = all(k in payload and payload[k] for k in ("provider", "model", "rubric_version"))
        has_trace = bool(by_entity[i]["tool_trace"])
        if not (has_fields and has_trace):
            incomplete.append(i)
    if incomplete:
        report.fail("audit_log completeness", f"missing provider/model/rubric_version/tool_trace on {len(incomplete)} rows")
    else:
        report.ok("audit_log for evaluations", f"all {len(ids)} evaluations have a complete audit_log row (provider, model, rubric_version, tool_trace)")


async def check_pii_redaction(report: Report) -> None:
    async with session_scope() as session:
        resumes = await fetch_all(session, "SELECT id, redacted_text FROM resumes")
    redacted_by_id = {str(r["id"]): (r["redacted_text"] or "") for r in resumes}

    leaks: list[str] = []
    missing: list[str] = []
    for meta in SEED_RESUME_MANIFEST:
        redacted = redacted_by_id.get(meta["id"])
        if redacted is None:
            missing.append(meta["candidate_label"])
            continue
        for field in ("name", "email", "phone"):
            value = meta.get(field)
            if value and value in redacted:
                leaks.append(f"{meta['candidate_label']}: {field} present in redacted_text")

    if missing:
        report.fail("PII redaction", f"resumes missing from DB: {missing}")
    if leaks:
        for leak in leaks:
            report.fail("PII redaction", leak)
    if not missing and not leaks:
        report.ok("PII redaction", f"no seeded candidate's name/email/phone found in redacted_text ({len(SEED_RESUME_MANIFEST)} resumes checked)")


# ---------------------------------------------------------------------------
# --probe mode
# ---------------------------------------------------------------------------


async def run_probe(report: Report) -> None:
    from app.config import settings

    if settings.llm_provider == "mock":
        report.fail("probe precondition", "LLM_PROVIDER=mock — set LLM_PROVIDER=openrouter (or another real provider) to run --probe")
        return

    from app.llm import provider as llm_provider

    health = await llm_provider.health()
    if not health.get("reachable"):
        report.fail("provider reachability", f"provider={health.get('provider')} has no API key configured / is unreachable")
        return
    report.ok("provider reachability", f"provider={health.get('provider')}, model={health.get('model')}")

    result = await llm_provider.probe_tool_calling()
    if result.get("ok"):
        report.ok("tool-calling probe", f"model {health.get('model')!r} on provider {result.get('provider')!r} supports tool calling")
    else:
        report.fail(
            "tool-calling probe",
            f"configured model {health.get('model')!r} on provider {result.get('provider')!r} does NOT support tool calling "
            f"(F2's agent is dead without this) — {result.get('error', 'no further detail')}",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _default_run(report: Report) -> None:
    chat_available = False
    app_main = None
    try:
        from app import main as app_main  # type: ignore[no-redef]

        app_main._mount_routers(app_main.app)
        chat_available = "chat" in app_main.routers_loaded
    except Exception as exc:  # noqa: BLE001
        report.skip("router discovery", f"could not import/mount app.main routers: {exc}")

    client: httpx.AsyncClient | None = None
    if chat_available and app_main is not None:
        transport = httpx.ASGITransport(app=app_main.app)
        client = httpx.AsyncClient(transport=transport, base_url="http://smoke-test")

    try:
        await check_policy_chat(report, client)
    finally:
        if client is not None:
            await client.aclose()

    eval_rows = await check_evaluations(report)
    await check_evidence_quotes(report, eval_rows)
    await check_audit_log(report, eval_rows)
    await check_pii_redaction(report)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="spend ~2 real LLM requests to verify the configured provider/model supports tool calling (gate-only)",
    )
    args = parser.parse_args()

    report = Report()
    if args.probe:
        await run_probe(report)
    else:
        await _default_run(report)

    print(report.render())
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
