/**
 * API contract — exact TypeScript mirror of `backend/app/schemas.py`.
 *
 * Owned by A0. Frozen after Wave 0. Field names are snake_case throughout
 * on purpose: this is what the JSON on the wire actually looks like, and
 * camelCasing it here would just be a silent translation layer nobody
 * asked for. Do not rename fields for "JS convention" — match the API.
 */

// ---------------------------------------------------------------------------
// Shared literals
// ---------------------------------------------------------------------------

export type Recommendation = "strong_yes" | "yes" | "maybe" | "no";
export type ResumeStatus = "queued" | "parsing" | "evaluating" | "scored" | "failed";

// ---------------------------------------------------------------------------
// F1 — Policy RAG Chat
// ---------------------------------------------------------------------------

export interface Citation {
  chunk_id: string;
  document_title: string;
  section: string;
  quote: string;
  start_char: number;
  end_char: number;
}

export interface Turn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  question: string;
  history?: Turn[];
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  grounded: boolean;
  provider: string;
  model: string;
}

// ---------------------------------------------------------------------------
// Documents (F1 ingestion)
// ---------------------------------------------------------------------------

export interface Document {
  id: string;
  title: string;
  kind: string;
  source_name: string | null;
  char_count: number | null;
  uploaded_at: string; // ISO 8601
}

export interface PolicyUploadResponse {
  document_id: string;
  chunk_count: number;
}

export interface DocumentsResponse {
  documents: Document[];
}

// ---------------------------------------------------------------------------
// F2 — Resume Evaluator Agent
// ---------------------------------------------------------------------------

export interface CriterionScore {
  criterion: string;
  score: number; // 0-10
  weight: number;
  evidence_quote: string | null;
  reasoning: string;
}

export interface EvalMeta {
  provider: string;
  model: string;
  rubric_version: string;
  prompt_version: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
}

export interface EvaluationResult {
  id: string;
  resume_id: string;
  job_id: string;
  fit_score: number; // 0-100
  recommendation: Recommendation;
  criteria: CriterionScore[];
  matched_skills: string[];
  gaps: string[];
  summary: string;
  meta: EvalMeta;
}

export interface RubricCriterion {
  criterion: string;
  weight: number;
}

export const RUBRIC_VERSION = "v1";

/** Mirrors `DEFAULT_RUBRIC` in `backend/app/schemas.py`. */
export const DEFAULT_RUBRIC: RubricCriterion[] = [
  { criterion: "Must-have skills coverage", weight: 0.3 },
  { criterion: "Relevant years of experience", weight: 0.2 },
  { criterion: "Domain / industry fit", weight: 0.15 },
  { criterion: "Seniority & scope of ownership", weight: 0.15 },
  { criterion: "Project & impact evidence", weight: 0.1 },
  { criterion: "Education / certifications", weight: 0.05 },
  { criterion: "Tenure stability", weight: 0.05 },
];

export interface EvaluateRequest {
  resume_id: string;
  job_id: string;
}

// ---------------------------------------------------------------------------
// Resumes (F2/F5 upload + board)
// ---------------------------------------------------------------------------

export interface Resume {
  id: string;
  candidate_label: string;
  file_name: string | null;
  raw_text: string | null;
  redacted_text: string | null;
  sections: Record<string, string>;
  status: ResumeStatus;
  error: string | null;
  uploaded_at: string;
}

export interface ResumeUploadItem {
  id: string;
  status: ResumeStatus;
}

export interface ResumesUploadResponse {
  resumes: ResumeUploadItem[];
}

// ---------------------------------------------------------------------------
// F3 — Jobs / JD Studio
// ---------------------------------------------------------------------------

export interface Job {
  id: string;
  title: string;
  level: string | null;
  location: string | null;
  department: string | null;
  description_md: string | null;
  must_haves: string[];
  nice_to_haves: string[];
  min_years: number | null;
  comp_min: number | null;
  comp_max: number | null;
  rubric_weights: RubricCriterion[] | null;
  created_at: string;
}

/** POST /api/jobs body — the 5-field form F3 generates a full JD from. */
export interface JobDraft {
  title: string;
  level?: string | null;
  must_haves?: string[];
  nice_to_haves?: string[];
  location?: string | null;
  department?: string | null;
  comp_min?: number | null;
  comp_max?: number | null;
  min_years?: number | null;
  rubric_weights?: RubricCriterion[] | null;
}

export interface JobCreateResponse {
  job: Job;
}

export interface JobsResponse {
  jobs: Job[];
}

export interface JobDetailResponse {
  job: Job;
  evaluations: EvaluationResult[];
}

// ---------------------------------------------------------------------------
// F4 — Interview Kit Generator
// ---------------------------------------------------------------------------

export interface InterviewQuestion {
  question: string;
  targets: string;
  good_answer: string;
  follow_up: string;
  scoring_anchor: string;
}

export interface InterviewKitRequest {
  evaluation_id: string;
}

export interface InterviewKitResponse {
  questions: InterviewQuestion[];
}

// ---------------------------------------------------------------------------
// F6 — Analytics
// ---------------------------------------------------------------------------

export interface PipelineStageCount {
  status: ResumeStatus;
  count: number;
}

export interface ScoreBucket {
  range_label: string;
  count: number;
}

export interface SkillCount {
  skill: string;
  count: number;
}

export interface SkillGaps {
  top_present: SkillCount[];
  top_missing: SkillCount[];
}

export interface UsageStat {
  provider: string;
  model: string;
  request_count: number;
  avg_latency_ms: number;
  total_input_tokens: number;
  total_output_tokens: number;
}

export interface AnalyticsResponse {
  pipeline: PipelineStageCount[];
  score_distribution: ScoreBucket[];
  skill_gaps: SkillGaps;
  usage: UsageStat[];
}

export interface AnalyticsQueryRequest {
  question: string;
}

export interface AnalyticsQueryResponse {
  sql: string;
  rows: Record<string, unknown>[];
  explanation: string;
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: string;
  db: boolean;
  llm_provider: string;
  llm_reachable: boolean;
  model?: string | null;
  tool_calling?: "ok" | "failed" | "unknown" | null;
  budget_used_today?: number | null;
  budget_limit?: number | null;
}

// ---------------------------------------------------------------------------
// Errors — {"error": {"code", "message"}} envelope
// ---------------------------------------------------------------------------

export interface ErrorBody {
  code: string;
  message: string;
  retry_after?: number | null;
}

export interface ErrorResponse {
  error: ErrorBody;
}
