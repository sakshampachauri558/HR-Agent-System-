/**
 * Evaluation Detail — PRD §4 F2's output rendered for a human reviewer,
 * demo step 7, plus the "Generate interview kit" hand-off to F4 (demo
 * step 8).
 *
 * Three things here carry the product's credibility (PRD's dispatch brief
 * calls these out explicitly):
 *   1. The "scored on redacted text" badge is shown directly beside the
 *      actual unredacted resume (fetched from `GET /api/resumes/{id}`), so
 *      the claim and the evidence sit together instead of one being an
 *      assertion the human has to trust.
 *   2. A criterion whose evidence quote failed the substring-verification
 *      check (flagged by the evaluator with a literal
 *      " [unverified: quote not found in redacted resume text]" suffix on
 *      `reasoning` -- the frozen `CriterionScore` schema has no dedicated
 *      `verified` field) renders with a visible warning, never silently
 *      identical to a verified row.
 *   3. `evidence_quote: null` renders as a neutral "Not evidenced" label,
 *      never as an empty string or an error state.
 *
 * Owned by A8 (Board + Interview Kit).
 */
import { useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, ApiError } from "../api";
import type { CriterionScore, InterviewQuestion, Recommendation, Resume } from "../types";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorBanner,
  Spinner,
} from "../components/ui";
import type { BadgeVariant } from "../components/ui";

// ---------------------------------------------------------------------------
// GET /api/resumes/{id} has no helper in the frozen api.ts -- it's an
// additive route (see routers/resumes.py's module docstring), not part of
// PRD §9's literal contract. This mirrors `request()` in api.ts exactly:
// same base-URL resolution, same `X-Demo-User` header, same
// `{error:{code,message}}` envelope thrown as `ApiError`.
// ---------------------------------------------------------------------------
const API_BASE: string =
  (import.meta as unknown as { env?: Record<string, string | undefined> }).env?.VITE_API_BASE ?? "";

async function fetchResume(resumeId: string): Promise<Resume> {
  const res = await fetch(`${API_BASE}/api/resumes/${encodeURIComponent(resumeId)}`, {
    headers: { "X-Demo-User": "demo-user" },
  });
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
    const envelope = (parsed ?? {}) as { error?: { code?: string; message?: string; retry_after?: number | null } };
    throw new ApiError(
      envelope.error?.code ?? "unknown_error",
      envelope.error?.message ?? res.statusText ?? `Request failed with status ${res.status}`,
      res.status,
      envelope.error?.retry_after ?? null
    );
  }
  return parsed as Resume;
}

// ---------------------------------------------------------------------------
// Verification — the cheap hallucination check (PRD §10).
// ---------------------------------------------------------------------------
const UNVERIFIED_SUFFIX = " [unverified: quote not found in redacted resume text]";

function splitVerification(reasoning: string): { text: string; unverified: boolean } {
  if (reasoning.endsWith(UNVERIFIED_SUFFIX)) {
    return { text: reasoning.slice(0, -UNVERIFIED_SUFFIX.length), unverified: true };
  }
  return { text: reasoning, unverified: false };
}

const RECOMMENDATION_LABEL: Record<Recommendation, string> = {
  strong_yes: "Strong yes",
  yes: "Yes",
  maybe: "Maybe",
  no: "No",
};

const RECOMMENDATION_VARIANT: Record<Recommendation, BadgeVariant> = {
  strong_yes: "success",
  yes: "info",
  maybe: "warning",
  no: "danger",
};

// ---------------------------------------------------------------------------
// Rubric row
// ---------------------------------------------------------------------------

function CriterionRow({ criterion }: { criterion: CriterionScore }) {
  const { text, unverified } = splitVerification(criterion.reasoning || "");
  return (
    <tr className="border-b border-[hsl(var(--border))] align-top last:border-0">
      <td className="py-3 pr-3 font-medium">{criterion.criterion}</td>
      <td className="py-3 pr-3 text-[hsl(var(--muted-foreground))]">{Math.round(criterion.weight * 100)}%</td>
      <td className="py-3 pr-3">
        <span className="font-semibold">{criterion.score}</span>
        <span className="text-[hsl(var(--muted-foreground))]">/10</span>
      </td>
      <td className="max-w-xs py-3 pr-3">
        {criterion.evidence_quote === null ? (
          <span className="text-xs italic text-[hsl(var(--muted-foreground))]">Not evidenced</span>
        ) : (
          <div className="flex flex-col gap-1.5">
            <blockquote
              className="rounded-md border-l-4 px-2 py-1 text-xs italic leading-relaxed text-[hsl(var(--foreground))]"
              style={{
                borderColor: unverified ? "hsl(var(--danger))" : "hsl(var(--border))",
                backgroundColor: unverified ? "hsl(var(--danger) / 0.08)" : "hsl(var(--muted))",
              }}
            >
              "{criterion.evidence_quote}"
            </blockquote>
            {unverified && (
              <Badge variant="danger" className="w-fit">
                Unverified — not found verbatim in the redacted resume
              </Badge>
            )}
          </div>
        )}
      </td>
      <td className="py-3 text-[hsl(var(--muted-foreground))]">{text}</td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Redaction badge + the unredacted/redacted resume side by side.
// ---------------------------------------------------------------------------

function ResumeComparisonPanel({ resumeId }: { resumeId: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["resume", resumeId],
    queryFn: () => fetchResume(resumeId),
  });
  const apiError = error as ApiError | null;

  return (
    <Card>
      <CardHeader className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Resume — redacted vs. unredacted</CardTitle>
          <Badge variant="info">Scored on redacted text</Badge>
        </div>
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          The evaluator model only ever saw the redacted version on the right — name, contact
          info, address, and other identity fields were stripped before scoring. The unredacted
          version on the left is shown here for the human reviewer only.
        </p>
      </CardHeader>
      <CardContent>
        {isLoading && (
          <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
            <Spinner size="sm" /> Loading resume…
          </div>
        )}
        {isError && (
          <ErrorBanner
            code={apiError?.code ?? "unknown_error"}
            message={apiError?.message ?? "Failed to load resume."}
            onRetry={() => refetch()}
          />
        )}
        {data && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="flex flex-col gap-1">
              <p className="text-xs font-semibold uppercase text-[hsl(var(--muted-foreground))]">
                Unredacted — human view only
              </p>
              <pre className="max-h-80 overflow-y-auto whitespace-pre-wrap rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))] p-3 text-xs leading-relaxed">
                {data.raw_text ?? "(no resume text on file)"}
              </pre>
            </div>
            <div className="flex flex-col gap-1">
              <p className="text-xs font-semibold uppercase text-[hsl(var(--muted-foreground))]">
                Redacted — what the model scored
              </p>
              <pre className="max-h-80 overflow-y-auto whitespace-pre-wrap rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))] p-3 text-xs leading-relaxed">
                {data.redacted_text ?? "(no redacted text on file)"}
              </pre>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Interview kit hand-off (F4, demo step 8).
// ---------------------------------------------------------------------------

function InterviewKitSection({ evaluationId }: { evaluationId: string }) {
  const mutation = useMutation({
    mutationFn: () => api.generateInterviewKit({ evaluation_id: evaluationId }),
  });
  const apiError = mutation.error as ApiError | null;
  const questions: InterviewQuestion[] = mutation.data?.questions ?? [];

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Interview kit</CardTitle>
        <Button size="sm" isLoading={mutation.isPending} onClick={() => mutation.mutate()}>
          {questions.length > 0 ? "Regenerate" : "Generate interview kit"}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {mutation.isPending && (
          <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
            <Spinner size="sm" /> Drafting questions targeted at this candidate's gaps…
          </div>
        )}
        {mutation.isError && (
          <ErrorBanner
            code={apiError?.code ?? "unknown_error"}
            message={apiError?.message ?? "Failed to generate the interview kit."}
          />
        )}
        {!mutation.isPending && questions.length === 0 && !mutation.isError && (
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            Generates 6-8 questions aimed at this candidate's specific gaps, plus two validating
            their claimed strengths. A second click is free — the kit is generated once per
            evaluation and reused.
          </p>
        )}
        {questions.map((q, i) => (
          <div key={i} className="flex flex-col gap-2 rounded-md border border-[hsl(var(--border))] p-3">
            <div className="flex items-start justify-between gap-3">
              <p className="text-sm font-medium">{q.question}</p>
              <Badge variant="neutral" className="shrink-0">
                {q.targets}
              </Badge>
            </div>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              <span className="font-semibold">Good answer:</span> {q.good_answer}
            </p>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              <span className="font-semibold">Follow-up:</span> {q.follow_up}
            </p>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              <span className="font-semibold">Scoring anchor:</span> {q.scoring_anchor}
            </p>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function EvalDetail() {
  const { id: evaluationId } = useParams<{ id: string }>();

  const evalQuery = useQuery({
    queryKey: ["evaluation", evaluationId],
    queryFn: () => api.getEvaluation(evaluationId as string),
    enabled: Boolean(evaluationId),
  });

  const apiError = evalQuery.error as ApiError | null;

  if (!evaluationId) {
    return <EmptyState title="No evaluation selected" description="Open one from a job's candidate board." />;
  }

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      {evalQuery.isLoading && (
        <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
          <Spinner size="sm" /> Loading evaluation…
        </div>
      )}

      {evalQuery.isError && (
        <ErrorBanner
          code={apiError?.code ?? "unknown_error"}
          message={apiError?.message ?? "Failed to load evaluation."}
          onRetry={() => evalQuery.refetch()}
        />
      )}

      {evalQuery.data && (
        <>
          <Card>
            <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <p className="text-xs text-[hsl(var(--muted-foreground))]">Evaluation</p>
                <div className="flex items-center gap-2">
                  <span className="text-3xl font-bold">{evalQuery.data.fit_score}</span>
                  <span className="text-sm text-[hsl(var(--muted-foreground))]">/100</span>
                  <Badge variant={RECOMMENDATION_VARIANT[evalQuery.data.recommendation]}>
                    {RECOMMENDATION_LABEL[evalQuery.data.recommendation]}
                  </Badge>
                </div>
              </div>
              <div className="text-right text-xs text-[hsl(var(--muted-foreground))]">
                <p>
                  {evalQuery.data.meta.provider} · {evalQuery.data.meta.model}
                </p>
                <p>
                  Rubric {evalQuery.data.meta.rubric_version} · Prompt {evalQuery.data.meta.prompt_version}
                </p>
              </div>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <p className="text-sm leading-relaxed">{evalQuery.data.summary}</p>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <p className="text-xs font-semibold uppercase text-[hsl(var(--muted-foreground))]">
                    Matched skills
                  </p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {evalQuery.data.matched_skills.length > 0 ? (
                      evalQuery.data.matched_skills.map((s) => (
                        <Badge key={s} variant="success">
                          {s}
                        </Badge>
                      ))
                    ) : (
                      <span className="text-xs text-[hsl(var(--muted-foreground))]">None</span>
                    )}
                  </div>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase text-[hsl(var(--muted-foreground))]">Gaps</p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {evalQuery.data.gaps.length > 0 ? (
                      evalQuery.data.gaps.map((g) => (
                        <Badge key={g} variant="warning">
                          {g}
                        </Badge>
                      ))
                    ) : (
                      <span className="text-xs text-[hsl(var(--muted-foreground))]">None</span>
                    )}
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Rubric</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-[hsl(var(--border))] text-left text-xs uppercase text-[hsl(var(--muted-foreground))]">
                      <th className="py-2 pr-3">Criterion</th>
                      <th className="py-2 pr-3">Weight</th>
                      <th className="py-2 pr-3">Score</th>
                      <th className="py-2 pr-3">Evidence</th>
                      <th className="py-2">Reasoning</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evalQuery.data.criteria.map((c) => (
                      <CriterionRow key={c.criterion} criterion={c} />
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>

          <ResumeComparisonPanel resumeId={evalQuery.data.resume_id} />

          <InterviewKitSection evaluationId={evalQuery.data.id} />
        </>
      )}
    </div>
  );
}
