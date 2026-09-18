/**
 * Candidate Ranking Board — PRD §4 F5 / Journey B, demo step 6.
 *
 * `/jobs/{id}` → table of evaluated candidates (sortable by fit score,
 * filterable by recommendation band, row click → `/eval/{id}`) → drop
 * resumes → `POST /api/resumes` → live status via SSE with a polling
 * fallback that keeps the board correct even when the stream drops → any
 * two candidates can be checked for a side-by-side compare view.
 *
 * Owned by A8 (Board + Interview Kit).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, ApiError, streamResumeStatus } from "../api";
import type { ResumeStreamEvent } from "../api";
import type { EvaluationResult, Job, Recommendation, ResumeStatus } from "../types";
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
// GET /api/jobs/{id} annotates each evaluation with `candidate_label`
// (joined from `resumes`) on top of the frozen `EvaluationResult` shape --
// see routers/jobs.py's module docstring for why. Same "extra additive
// field" pattern Jobs.tsx uses for `candidate_count`.
// ---------------------------------------------------------------------------
interface BoardEvaluation extends EvaluationResult {
  candidate_label: string;
}

interface JobDetailWithLabels {
  job: Job;
  evaluations: BoardEvaluation[];
}

type BandFilter = "all" | Recommendation;

interface PendingResume {
  fileName: string;
  status: ResumeStatus;
  error?: string | null;
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

const RESUME_STATUS_LABEL: Record<ResumeStatus, string> = {
  queued: "Queued",
  parsing: "Parsing",
  evaluating: "Evaluating",
  scored: "Scored",
  failed: "Failed",
};

const RESUME_STATUS_VARIANT: Record<ResumeStatus, BadgeVariant> = {
  queued: "neutral",
  parsing: "info",
  evaluating: "warning",
  scored: "success",
  failed: "danger",
};

// ---------------------------------------------------------------------------
// Side-by-side compare (PRD §15 cut line #3 — built, but last, per the
// dispatch brief).
// ---------------------------------------------------------------------------

function CompareView({ evaluations, onClose }: { evaluations: BoardEvaluation[]; onClose: () => void }) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Compare candidates</CardTitle>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </CardHeader>
      <CardContent className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {evaluations.map((ev) => (
          <div key={ev.id} className="flex flex-col gap-3 rounded-md border border-[hsl(var(--border))] p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold">{ev.candidate_label}</span>
              <Badge variant={RECOMMENDATION_VARIANT[ev.recommendation]}>
                {RECOMMENDATION_LABEL[ev.recommendation]}
              </Badge>
            </div>
            <div>
              <span className="text-2xl font-bold">{ev.fit_score}</span>
              <span className="text-sm text-[hsl(var(--muted-foreground))]">/100</span>
            </div>
            <p className="text-xs leading-relaxed text-[hsl(var(--muted-foreground))]">{ev.summary}</p>

            <div>
              <p className="text-xs font-semibold uppercase text-[hsl(var(--muted-foreground))]">Matched skills</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {ev.matched_skills.length > 0 ? (
                  ev.matched_skills.map((s) => (
                    <Badge key={s} variant="success" className="text-[10px]">
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
                {ev.gaps.length > 0 ? (
                  ev.gaps.map((g) => (
                    <Badge key={g} variant="warning" className="text-[10px]">
                      {g}
                    </Badge>
                  ))
                ) : (
                  <span className="text-xs text-[hsl(var(--muted-foreground))]">None</span>
                )}
              </div>
            </div>

            <div className="flex flex-col gap-1">
              <p className="text-xs font-semibold uppercase text-[hsl(var(--muted-foreground))]">Rubric</p>
              {ev.criteria.map((c) => (
                <div key={c.criterion} className="flex items-center justify-between gap-2 text-xs">
                  <span className="truncate">{c.criterion}</span>
                  <span className="shrink-0 font-medium">{c.score}/10</span>
                </div>
              ))}
            </div>

            <Link
              to={`/eval/${ev.id}`}
              className="text-xs font-medium text-[hsl(var(--accent))] hover:underline"
            >
              View full evaluation →
            </Link>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function JobBoard() {
  const { id: jobId } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [pendingResumes, setPendingResumes] = useState<Record<string, PendingResume>>({});
  const [bandFilter, setBandFilter] = useState<BandFilter>("all");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [showCompare, setShowCompare] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Any resume we're still watching that hasn't reached a terminal
  // "scored"/"failed" state keeps the 2s poll below alive -- this is what
  // makes the board converge to the true state even if SSE never delivers
  // a single frame (PRD §15 cut line #4 is the poll; we run both).
  const hasActiveUploads = Object.values(pendingResumes).some((p) => p.status !== "failed");

  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId as string) as Promise<JobDetailWithLabels>,
    enabled: Boolean(jobId),
    refetchInterval: hasActiveUploads ? 2000 : false,
  });

  // Once a pending upload's resume_id shows up among the job's evaluations,
  // it has reached "scored" and belongs in the main table, not the
  // "processing" list.
  useEffect(() => {
    if (!jobQuery.data) return;
    const scoredIds = new Set(jobQuery.data.evaluations.map((e) => e.resume_id));
    setPendingResumes((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const rid of Object.keys(prev)) {
        if (scoredIds.has(rid)) {
          delete next[rid];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [jobQuery.data]);

  // SSE is a cosmetic accelerant for the queued -> parsing -> evaluating
  // animation. It is NOT load-bearing for correctness: the 2s poll above
  // runs independently of whether this stream ever connects, drops, or
  // goes quiet, so the board's final state never depends on it.
  useEffect(() => {
    if (!jobId) return undefined;
    const unsubscribe = streamResumeStatus(
      jobId,
      (event: ResumeStreamEvent) => {
        // The backend's publisher currently emits `resume_id`, not `id`
        // (see routers/resumes.py `_publish`) -- read both defensively so
        // this keeps working whichever field name is actually on the wire.
        const raw = event as unknown as {
          id?: string;
          resume_id?: string;
          status: ResumeStatus;
          error?: string | null;
        };
        const rid = raw.resume_id ?? raw.id;
        if (!rid) return;
        setPendingResumes((prev) => {
          if (!(rid in prev)) return prev; // not a resume we're tracking this session
          return { ...prev, [rid]: { ...prev[rid], status: raw.status, error: raw.error ?? null } };
        });
      },
      () => {
        // Stream errored/closed -- nothing to do, the poll is the source of truth.
      }
    );
    return unsubscribe;
  }, [jobId]);

  const uploadMutation = useMutation({
    mutationFn: (files: File[]) => api.uploadResumes(jobId as string, files),
    onSuccess: (data, files) => {
      setPendingResumes((prev) => {
        const next = { ...prev };
        data.resumes.forEach((item, i) => {
          next[item.id] = { fileName: files[i]?.name ?? "resume", status: item.status, error: null };
        });
        return next;
      });
    },
  });

  function handleFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    uploadMutation.mutate(Array.from(fileList));
  }

  function toggleCompare(id: string) {
    setCompareIds((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= 2) return prev;
      return [...prev, id];
    });
  }

  const evaluations = useMemo(() => jobQuery.data?.evaluations ?? [], [jobQuery.data]);

  const bandCounts = useMemo(() => {
    const counts: Record<Recommendation, number> = { strong_yes: 0, yes: 0, maybe: 0, no: 0 };
    for (const e of evaluations) counts[e.recommendation] += 1;
    return counts;
  }, [evaluations]);

  const visibleRows = useMemo(() => {
    const filtered = bandFilter === "all" ? evaluations : evaluations.filter((r) => r.recommendation === bandFilter);
    return [...filtered].sort((a, b) => (sortDir === "desc" ? b.fit_score - a.fit_score : a.fit_score - b.fit_score));
  }, [evaluations, bandFilter, sortDir]);

  const compareEvaluations = compareIds
    .map((id) => evaluations.find((e) => e.id === id))
    .filter((e): e is BoardEvaluation => Boolean(e));

  const apiError = jobQuery.error as ApiError | null;
  const uploadError = uploadMutation.error as ApiError | null;

  if (!jobId) {
    return <EmptyState title="No job selected" description="Choose a job from the jobs list." />;
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Link to="/jobs" className="text-xs text-[hsl(var(--muted-foreground))] hover:underline">
          ← All jobs
        </Link>
        <h1 className="text-xl font-semibold">{jobQuery.data?.job.title ?? "Job"}</h1>
        <p className="text-sm text-[hsl(var(--muted-foreground))]">
          {jobQuery.data?.job.level ?? "Level not set"} · {jobQuery.data?.job.location ?? "Location not set"}
        </p>
      </div>

      {jobQuery.isLoading && (
        <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
          <Spinner size="sm" /> Loading job…
        </div>
      )}

      {jobQuery.isError && (
        <ErrorBanner
          code={apiError?.code ?? "unknown_error"}
          message={apiError?.message ?? "Failed to load job."}
          onRetry={() => jobQuery.refetch()}
        />
      )}

      {jobQuery.data && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Upload resumes</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div
                onDrop={(e) => {
                  e.preventDefault();
                  setIsDragging(false);
                  handleFiles(e.dataTransfer.files);
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  setIsDragging(true);
                }}
                onDragLeave={() => setIsDragging(false)}
                className={`flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-6 text-center transition-colors ${
                  isDragging ? "border-[hsl(var(--accent))] bg-[hsl(var(--accent)/0.06)]" : "border-[hsl(var(--border))]"
                }`}
              >
                <p className="text-sm text-[hsl(var(--muted-foreground))]">Drop resume files here, or</p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  isLoading={uploadMutation.isPending}
                  onClick={() => fileInputRef.current?.click()}
                >
                  Choose files
                </Button>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept=".pdf,.md,.txt"
                  className="hidden"
                  onChange={(e) => {
                    handleFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
              </div>
              {uploadMutation.isError && (
                <ErrorBanner
                  code={uploadError?.code ?? "unknown_error"}
                  message={uploadError?.message ?? "Upload failed."}
                />
              )}
            </CardContent>
          </Card>

          {Object.keys(pendingResumes).length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Processing uploads</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-2">
                {Object.entries(pendingResumes).map(([rid, p]) => (
                  <div key={rid} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span className="truncate">{p.fileName}</span>
                    <div className="flex items-center gap-2">
                      {p.status !== "scored" && p.status !== "failed" && <Spinner size="sm" />}
                      <Badge variant={RESUME_STATUS_VARIANT[p.status]}>{RESUME_STATUS_LABEL[p.status]}</Badge>
                    </div>
                    {p.status === "failed" && p.error && (
                      <span className="w-full text-xs text-[hsl(var(--danger))]">{p.error}</span>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          )}

          {evaluations.length === 0 ? (
            <EmptyState
              title="No candidates yet"
              description="Drop resumes above to start screening them against this job."
            />
          ) : (
            <Card>
              <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <CardTitle>Candidates ({evaluations.length})</CardTitle>
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={bandFilter}
                    onChange={(e) => setBandFilter(e.target.value as BandFilter)}
                    className="rounded-md border border-[hsl(var(--border))] bg-transparent px-2 py-1 text-xs text-[hsl(var(--foreground))]"
                  >
                    <option value="all">All bands ({evaluations.length})</option>
                    <option value="strong_yes">Strong yes ({bandCounts.strong_yes})</option>
                    <option value="yes">Yes ({bandCounts.yes})</option>
                    <option value="maybe">Maybe ({bandCounts.maybe})</option>
                    <option value="no">No ({bandCounts.no})</option>
                  </select>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setSortDir((d) => (d === "desc" ? "asc" : "desc"))}
                  >
                    Fit score {sortDir === "desc" ? "↓" : "↑"}
                  </Button>
                  {compareIds.length === 2 && (
                    <Button variant="secondary" size="sm" onClick={() => setShowCompare(true)}>
                      Compare selected
                    </Button>
                  )}
                </div>
              </CardHeader>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-[hsl(var(--border))] text-left text-xs uppercase text-[hsl(var(--muted-foreground))]">
                      <th className="w-8 py-2 pl-4"></th>
                      <th className="py-2 pr-3">Candidate</th>
                      <th className="py-2 pr-3">Fit score</th>
                      <th className="py-2 pr-3">Recommendation</th>
                      <th className="py-2 pr-3">Top matched skills</th>
                      <th className="py-2 pr-4">Top gaps</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[hsl(var(--border))]">
                    {visibleRows.map((row) => (
                      <tr
                        key={row.id}
                        onClick={() => navigate(`/eval/${row.id}`)}
                        className="cursor-pointer transition-colors hover:bg-[hsl(var(--muted))]"
                      >
                        <td className="py-2 pl-4" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={compareIds.includes(row.id)}
                            disabled={!compareIds.includes(row.id) && compareIds.length >= 2}
                            onChange={() => toggleCompare(row.id)}
                            aria-label={`Select ${row.candidate_label} for comparison`}
                          />
                        </td>
                        <td className="py-2 pr-3 font-medium">{row.candidate_label}</td>
                        <td className="py-2 pr-3 font-semibold">{row.fit_score}</td>
                        <td className="py-2 pr-3">
                          <Badge variant={RECOMMENDATION_VARIANT[row.recommendation]}>
                            {RECOMMENDATION_LABEL[row.recommendation]}
                          </Badge>
                        </td>
                        <td className="py-2 pr-3">
                          <div className="flex flex-wrap gap-1">
                            {row.matched_skills.slice(0, 3).map((s) => (
                              <Badge key={s} variant="success" className="text-[10px]">
                                {s}
                              </Badge>
                            ))}
                            {row.matched_skills.length === 0 && (
                              <span className="text-xs text-[hsl(var(--muted-foreground))]">None</span>
                            )}
                          </div>
                        </td>
                        <td className="py-2 pr-4">
                          <div className="flex flex-wrap gap-1">
                            {row.gaps.slice(0, 3).map((g) => (
                              <Badge key={g} variant="warning" className="text-[10px]">
                                {g}
                              </Badge>
                            ))}
                            {row.gaps.length === 0 && (
                              <span className="text-xs text-[hsl(var(--muted-foreground))]">None</span>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          {showCompare && compareEvaluations.length === 2 && (
            <CompareView evaluations={compareEvaluations} onClose={() => setShowCompare(false)} />
          )}
        </>
      )}
    </div>
  );
}
