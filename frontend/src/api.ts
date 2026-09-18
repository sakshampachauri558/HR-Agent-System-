/**
 * Typed fetch client for every route in PRD §9. Owned by A0, frozen after
 * Wave 0. Wave-1/2 pages import `api` (and, for the board, `subscribeSse`
 * / `streamResumeStatus`) from here rather than calling `fetch` directly,
 * so the error envelope and base-URL handling stay in one place.
 */

import type {
  AnalyticsQueryRequest,
  AnalyticsQueryResponse,
  AnalyticsResponse,
  ChatRequest,
  ChatResponse,
  DocumentsResponse,
  EvaluateRequest,
  EvaluationResult,
  HealthResponse,
  InterviewKitRequest,
  InterviewKitResponse,
  JobCreateResponse,
  JobDetailResponse,
  JobDraft,
  JobsResponse,
  PolicyUploadResponse,
  ResumeStatus,
  ResumesUploadResponse,
} from "./types";

/**
 * Vite injects `import.meta.env` at build time; we read it defensively
 * (rather than relying on a global `vite/client` ambient declaration
 * living in some other agent's file) so this module type-checks on its
 * own under `tsc --noEmit`.
 */
const API_BASE: string =
  (import.meta as unknown as { env?: Record<string, string | undefined> }).env
    ?.VITE_API_BASE ?? "";

/** Single hardcoded demo org (PRD §2 non-goals) — every request carries
 * this instead of an Authorization header. */
const DEMO_USER = "demo-user";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryAfter: number | null;

  constructor(code: string, message: string, status: number, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string; retry_after?: number | null };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (!isFormData && init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-Demo-User", DEMO_USER);

  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  const raw = await res.text();
  let parsed: unknown = null;
  if (raw) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = raw;
    }
  }

  if (!res.ok) {
    const envelope = (parsed ?? {}) as ErrorEnvelope;
    throw new ApiError(
      envelope.error?.code ?? "unknown_error",
      envelope.error?.message ?? res.statusText ?? `Request failed with status ${res.status}`,
      res.status,
      envelope.error?.retry_after ?? null,
    );
  }

  return parsed as T;
}

function toJsonInit(body: unknown, init: RequestInit = {}): RequestInit {
  return { ...init, method: init.method ?? "POST", body: JSON.stringify(body) };
}

export const api = {
  // --- Health -----------------------------------------------------------
  health: (): Promise<HealthResponse> => request<HealthResponse>("/api/health"),

  // --- F1 Policy RAG ------------------------------------------------------
  uploadPolicy: (file: File): Promise<PolicyUploadResponse> => {
    const form = new FormData();
    form.append("file", file);
    return request<PolicyUploadResponse>("/api/policies", { method: "POST", body: form });
  },

  listPolicies: (): Promise<DocumentsResponse> => request<DocumentsResponse>("/api/policies"),

  chat: (body: ChatRequest): Promise<ChatResponse> =>
    request<ChatResponse>("/api/chat", toJsonInit(body)),

  // --- F3 Jobs / JD Studio ------------------------------------------------
  createJob: (draft: JobDraft): Promise<JobCreateResponse> =>
    request<JobCreateResponse>("/api/jobs", toJsonInit(draft)),

  listJobs: (): Promise<JobsResponse> => request<JobsResponse>("/api/jobs"),

  getJob: (jobId: string): Promise<JobDetailResponse> =>
    request<JobDetailResponse>(`/api/jobs/${encodeURIComponent(jobId)}`),

  // --- F2/F5 Resumes -------------------------------------------------------
  uploadResumes: (jobId: string, files: File[]): Promise<ResumesUploadResponse> => {
    const form = new FormData();
    form.append("job_id", jobId);
    for (const file of files) form.append("files", file);
    return request<ResumesUploadResponse>("/api/resumes", { method: "POST", body: form });
  },

  evaluate: (body: EvaluateRequest): Promise<EvaluationResult> =>
    request<EvaluationResult>("/api/evaluate", toJsonInit(body)),

  getEvaluation: (evaluationId: string): Promise<EvaluationResult> =>
    request<EvaluationResult>(`/api/evaluations/${encodeURIComponent(evaluationId)}`),

  // --- F4 Interview Kit -----------------------------------------------------
  generateInterviewKit: (body: InterviewKitRequest): Promise<InterviewKitResponse> =>
    request<InterviewKitResponse>("/api/interview-kit", toJsonInit(body)),

  // --- F6 Analytics ---------------------------------------------------------
  getAnalytics: (): Promise<AnalyticsResponse> => request<AnalyticsResponse>("/api/analytics"),

  queryAnalytics: (body: AnalyticsQueryRequest): Promise<AnalyticsQueryResponse> =>
    request<AnalyticsQueryResponse>("/api/analytics/query", toJsonInit(body)),
};

// ---------------------------------------------------------------------------
// SSE helpers
// ---------------------------------------------------------------------------

/**
 * Generic SSE subscription. Returns an unsubscribe function — call it from
 * a `useEffect` cleanup. Frames that aren't valid JSON are dropped rather
 * than thrown, since a keep-alive comment/ping frame is a normal thing for
 * an SSE stream to send.
 */
export function subscribeSse<T = unknown>(
  path: string,
  onEvent: (data: T) => void,
  onError?: (err: Event) => void,
): () => void {
  const source = new EventSource(`${API_BASE}${path}`);
  source.onmessage = (evt: MessageEvent<string>) => {
    if (!evt.data) return;
    try {
      onEvent(JSON.parse(evt.data) as T);
    } catch {
      // Non-JSON frame (e.g. a keep-alive comment) — ignore.
    }
  };
  if (onError) source.onerror = onError;
  return () => source.close();
}

export interface ResumeStreamEvent {
  id: string;
  status: ResumeStatus;
  error?: string | null;
}

/** SSE helper for GET /api/resumes/stream?job_id= — the board's live
 * status feed (F5). Falls back gracefully: if the browser/environment
 * never receives a frame, the board should still poll `api.getJob` every
 * couple of seconds (PRD §15 cut line #4). */
export function streamResumeStatus(
  jobId: string,
  onEvent: (event: ResumeStreamEvent) => void,
  onError?: (err: Event) => void,
): () => void {
  return subscribeSse<ResumeStreamEvent>(
    `/api/resumes/stream?job_id=${encodeURIComponent(jobId)}`,
    onEvent,
    onError,
  );
}
