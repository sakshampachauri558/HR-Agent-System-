"""API contract — the source of truth for `frontend/src/types.ts`.

Owned by A0. **Frozen after Wave 0.** Every Wave-1/2 agent imports from
here and must not edit it. Field names are snake_case throughout because
that is what actually goes over the wire — `types.ts` mirrors these names
verbatim rather than camelCasing them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared literals
# ---------------------------------------------------------------------------

Recommendation = Literal["strong_yes", "yes", "maybe", "no"]
ResumeStatus = Literal["queued", "parsing", "evaluating", "scored", "failed"]

# ---------------------------------------------------------------------------
# F1 — Policy RAG Chat
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    chunk_id: UUID
    document_title: str
    section: str
    quote: str
    start_char: int
    end_char: int


class Turn(BaseModel):
    """One turn of chat history."""

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    question: str
    history: list[Turn] = []


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    grounded: bool
    provider: str
    model: str


# ---------------------------------------------------------------------------
# Documents (F1 ingestion)
# ---------------------------------------------------------------------------


class Document(BaseModel):
    id: UUID
    title: str
    kind: str
    source_name: str | None = None
    char_count: int | None = None
    uploaded_at: datetime


class PolicyUploadResponse(BaseModel):
    document_id: UUID
    chunk_count: int


class DocumentsResponse(BaseModel):
    documents: list[Document]


# ---------------------------------------------------------------------------
# F2 — Resume Evaluator Agent
# ---------------------------------------------------------------------------


class CriterionScore(BaseModel):
    criterion: str
    score: int = Field(ge=0, le=10)
    weight: float
    evidence_quote: str | None = None  # verbatim from redacted resume, or None
    reasoning: str = Field(max_length=300)


class EvalMeta(BaseModel):
    """Mirrors the audit columns on `evaluations` — provider, model,
    latency, tokens, and the versions the score was produced under."""

    provider: str
    model: str
    rubric_version: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class EvaluationResult(BaseModel):
    id: UUID
    resume_id: UUID
    job_id: UUID
    fit_score: int = Field(ge=0, le=100)
    recommendation: Recommendation
    criteria: list[CriterionScore]
    matched_skills: list[str]
    gaps: list[str]
    summary: str
    meta: EvalMeta  # provider, model, latency_ms, tokens


class RubricCriterion(BaseModel):
    criterion: str
    weight: float


RUBRIC_VERSION = "v1"

# PRD §4 F2 default rubric. `jobs.rubric_weights` overrides this per job;
# when null, callers fall back to this constant.
DEFAULT_RUBRIC: list[RubricCriterion] = [
    RubricCriterion(criterion="Must-have skills coverage", weight=0.30),
    RubricCriterion(criterion="Relevant years of experience", weight=0.20),
    RubricCriterion(criterion="Domain / industry fit", weight=0.15),
    RubricCriterion(criterion="Seniority & scope of ownership", weight=0.15),
    RubricCriterion(criterion="Project & impact evidence", weight=0.10),
    RubricCriterion(criterion="Education / certifications", weight=0.05),
    RubricCriterion(criterion="Tenure stability", weight=0.05),
]


class EvaluateRequest(BaseModel):
    resume_id: UUID
    job_id: UUID


# ---------------------------------------------------------------------------
# Resumes (F2/F5 upload + board)
# ---------------------------------------------------------------------------


class Resume(BaseModel):
    id: UUID
    candidate_label: str
    file_name: str | None = None
    raw_text: str | None = None
    redacted_text: str | None = None
    sections: dict[str, str] = {}
    status: ResumeStatus
    error: str | None = None
    uploaded_at: datetime


class ResumeUploadItem(BaseModel):
    id: UUID
    status: ResumeStatus


class ResumesUploadResponse(BaseModel):
    resumes: list[ResumeUploadItem]


# ---------------------------------------------------------------------------
# F3 — Jobs / JD Studio
# ---------------------------------------------------------------------------


class Job(BaseModel):
    id: UUID
    title: str
    level: str | None = None
    location: str | None = None
    department: str | None = None
    description_md: str | None = None
    must_haves: list[str] = []
    nice_to_haves: list[str] = []
    min_years: int | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    rubric_weights: list[RubricCriterion] | None = None
    created_at: datetime


class JobDraft(BaseModel):
    """POST /api/jobs body — the 5-field form F3 generates a full JD from."""

    title: str
    level: str | None = None
    must_haves: list[str] = []
    nice_to_haves: list[str] = []
    location: str | None = None
    department: str | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    min_years: int | None = None
    rubric_weights: list[RubricCriterion] | None = None


class JobCreateResponse(BaseModel):
    job: Job


class JobsResponse(BaseModel):
    jobs: list[Job]


class JobDetailResponse(BaseModel):
    job: Job
    evaluations: list[EvaluationResult]


# ---------------------------------------------------------------------------
# F4 — Interview Kit Generator
# ---------------------------------------------------------------------------


class InterviewQuestion(BaseModel):
    question: str
    targets: str  # which gap/strength it probes
    good_answer: str
    follow_up: str
    scoring_anchor: str


class InterviewKitRequest(BaseModel):
    evaluation_id: UUID


class InterviewKitResponse(BaseModel):
    questions: list[InterviewQuestion]


# ---------------------------------------------------------------------------
# F6 — Analytics
# ---------------------------------------------------------------------------


class PipelineStageCount(BaseModel):
    status: ResumeStatus
    count: int


class ScoreBucket(BaseModel):
    range_label: str  # e.g. "70-79"
    count: int


class SkillCount(BaseModel):
    skill: str
    count: int


class SkillGaps(BaseModel):
    top_present: list[SkillCount]
    top_missing: list[SkillCount]


class UsageStat(BaseModel):
    provider: str
    model: str
    request_count: int
    avg_latency_ms: float
    total_input_tokens: int
    total_output_tokens: int


class AnalyticsResponse(BaseModel):
    pipeline: list[PipelineStageCount]
    score_distribution: list[ScoreBucket]
    skill_gaps: SkillGaps
    usage: list[UsageStat]


class AnalyticsQueryRequest(BaseModel):
    question: str


class AnalyticsQueryResponse(BaseModel):
    sql: str
    rows: list[dict[str, Any]]
    explanation: str


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    db: bool
    llm_provider: str
    llm_reachable: bool
    # Not in the minimal §9 contract but populated by app.llm.provider.health()
    # and shown on /admin and in the demo script; additive, never breaks a
    # client only reading the four required fields above.
    model: str | None = None
    tool_calling: Literal["ok", "failed", "unknown"] | None = None
    budget_used_today: int | None = None
    budget_limit: int | None = None


# ---------------------------------------------------------------------------
# Errors — {"error": {"code", "message"}} envelope
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str
    message: str
    # Only populated for code == "llm_rate_limited" / "llm_budget_exhausted".
    retry_after: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
