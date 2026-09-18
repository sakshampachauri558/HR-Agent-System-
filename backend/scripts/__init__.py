"""Shared constants for `scripts.seed` and `scripts.smoke`.

Both modules import from here so resume/job/evaluation identifiers, and
the seed data directory, are defined exactly once. Real candidate PII
(name/email/phone/address) is duplicated here rather than scraped back out
of the markdown at runtime, so `scripts.smoke`'s PII-leak assertion does
not need to know anything about the seed resumes' markdown format -- it
just checks these canonical values are absent from `redacted_text`.

Owned by A7 (Seed + Smoke). See `seed/**` for the actual fixture content
these identifiers point at.
"""

from __future__ import annotations

from pathlib import Path


def _resolve_seed_dir() -> Path:
    """Same resolution strategy as `app.llm.mock._resolve_fixtures_path`:
    prefer the read-only bind mount used by docker-compose.override.yml
    (`/app/seed`), fall back to a path relative to this file (for a bare
    `pytest`/script run outside Docker), then finally cwd/seed.
    """
    candidates = [
        Path("/app/seed"),
        Path(__file__).resolve().parents[2] / "seed",
        Path.cwd() / "seed",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # Last resort: return the container path even if it doesn't exist yet
    # so callers get a clear, specific FileNotFoundError instead of a
    # confusing relative-path error.
    return candidates[0]


SEED_DIR = _resolve_seed_dir()
POLICIES_DIR = SEED_DIR / "policies"
RESUMES_DIR = SEED_DIR / "resumes"
JOBS_PATH = SEED_DIR / "jobs.json"
EVALUATIONS_PATH = SEED_DIR / "evaluations.json"
QUESTIONS_PATH = SEED_DIR / "questions.json"

# ---------------------------------------------------------------------------
# Jobs (seed/jobs.json) -- ids repeated here so seed.py and smoke.py never
# have to guess which job the 5 seed evaluations were scored against.
# ---------------------------------------------------------------------------
SEED_JOB_SENIOR_BACKEND_ID = "a0000000-0000-4000-8000-000000000001"
SEED_JOB_PRODUCT_MANAGER_ID = "a0000000-0000-4000-8000-000000000002"

# ---------------------------------------------------------------------------
# Policy documents (seed/policies/*.md). `source_name` is the idempotency
# key seed.py deletes-and-reinserts on, regardless of which ingestion tier
# actually ran.
# ---------------------------------------------------------------------------
SEED_DOCUMENT_MANIFEST: list[dict[str, str]] = [
    {"title": "Leave Policy", "source_name": "leave-policy.md", "kind": "md"},
    {"title": "Remote Work Policy", "source_name": "remote-work-policy.md", "kind": "md"},
    {"title": "Reimbursement Policy", "source_name": "reimbursement-policy.md", "kind": "md"},
]

# ---------------------------------------------------------------------------
# Resumes (seed/resumes/*.md). `name`/`email`/`phone`/`address` mirror
# exactly what's written into each markdown file's header block -- both
# seed.py's fallback redactor (used only if `app.redact` hasn't landed
# yet) and smoke.py's PII-leak assertion check these same canonical
# values, independent of whichever redaction implementation actually ran.
# ---------------------------------------------------------------------------
SEED_RESUME_MANIFEST: list[dict[str, str]] = [
    {
        "id": "b0000000-0000-4000-8000-000000000001",
        "file_name": "priya-sharma.md",
        "candidate_label": "Candidate A",
        "tier": "strong",
        "name": "Priya Sharma",
        "email": "priya.sharma1990@examplemail.test",
        "phone": "+91-98765-43210",
        "address": "45, Indiranagar 100 Feet Road, Bengaluru, Karnataka 560038",
    },
    {
        "id": "b0000000-0000-4000-8000-000000000002",
        "file_name": "rohan-verma.md",
        "candidate_label": "Candidate B",
        "tier": "middling",
        "name": "Rohan Verma",
        "email": "rohan.verma88@examplemail.test",
        "phone": "+91-98220-11223",
        "address": "12, Kothrud, Pune, Maharashtra 411038",
    },
    {
        "id": "b0000000-0000-4000-8000-000000000003",
        "file_name": "ananya-iyer.md",
        "candidate_label": "Candidate C",
        "tier": "middling",
        "name": "Ananya Iyer",
        "email": "ananya.iyer@examplemail.test",
        "phone": "+91-90031-55678",
        "address": "7, Adyar, Chennai, Tamil Nadu 600020",
    },
    {
        "id": "b0000000-0000-4000-8000-000000000004",
        "file_name": "karan-mehta.md",
        "candidate_label": "Candidate D",
        "tier": "weak",
        "name": "Karan Mehta",
        "email": "karan.mehta.dev@examplemail.test",
        "phone": "+91-97171-99887",
        "address": "23, Sector 15, Gurugram, Haryana 122001",
    },
    {
        "id": "b0000000-0000-4000-8000-000000000005",
        "file_name": "sana-fernandes.md",
        "candidate_label": "Candidate E",
        "tier": "adjacent",
        "name": "Sana Fernandes",
        "email": "sana.fernandes@examplemail.test",
        "phone": "+91-96323-44556",
        "address": "88, Bandra West, Mumbai, Maharashtra 400050",
    },
]

# ---------------------------------------------------------------------------
# Evaluations (seed/evaluations.json). Deterministic audit_log ids, keyed
# by evaluation id, so re-running `scripts.seed` upserts the same
# audit_log row instead of appending a new one each time.
# ---------------------------------------------------------------------------
SEED_EVALUATION_AUDIT_IDS: dict[str, str] = {
    "c0000000-0000-4000-8000-000000000001": "d0000000-0000-4000-8000-000000000001",
    "c0000000-0000-4000-8000-000000000002": "d0000000-0000-4000-8000-000000000002",
    "c0000000-0000-4000-8000-000000000003": "d0000000-0000-4000-8000-000000000003",
    "c0000000-0000-4000-8000-000000000004": "d0000000-0000-4000-8000-000000000004",
    "c0000000-0000-4000-8000-000000000005": "d0000000-0000-4000-8000-000000000005",
}
