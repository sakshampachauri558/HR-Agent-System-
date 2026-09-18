import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";

import { api, ApiError } from "../api";
import type { Job, JobDraft } from "../types";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
  ErrorBanner,
  Spinner,
} from "../components/ui";

/**
 * F3 JD generation is a single LLM request that folds the inclusive-
 * language pass into the same output (see backend/app/agents/jd.py). The
 * frozen `JobCreateResponse` contract only has room for `{job: Job}`, so
 * the backend adds `inclusive_language_flags` as an additive top-level
 * field on top of that -- same pattern `Shell.tsx` uses for `/api/health`
 * fields the minimal `HealthResponse` doesn't declare.
 */
interface InclusiveLanguageFlag {
  phrase: string;
  reason: string;
  suggestion: string;
}

interface CreateJobResponse {
  job: Job;
  inclusive_language_flags?: InclusiveLanguageFlag[];
}

function parseList(value: string): string[] {
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

const FIELD_CLASSES =
  "rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm text-[hsl(var(--foreground))] " +
  "placeholder:text-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-2 focus:ring-[hsl(var(--accent))]";

/**
 * Tiny, dependency-free renderer for the `##`/`###`/`- ` Markdown the JD
 * agent emits in `description_md`. No Markdown library is wired into this
 * frontend (see package.json), and adding one is A1's call, not this
 * page's -- this covers the handful of constructs the generation prompt
 * actually produces.
 */
function renderJdMarkdown(markdown: string): React.ReactElement[] {
  const lines = markdown.split("\n");
  const blocks: React.ReactElement[] = [];
  let listItems: string[] = [];

  const flushList = () => {
    if (listItems.length === 0) return;
    blocks.push(
      <ul key={`ul-${blocks.length}`} className="list-disc space-y-1 pl-5">
        {listItems.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    );
    listItems = [];
  };

  lines.forEach((rawLine, i) => {
    const line = rawLine.trim();
    if (line.startsWith("### ")) {
      flushList();
      blocks.push(
        <h4 key={i} className="mt-3 text-sm font-semibold">
          {line.slice(4)}
        </h4>
      );
    } else if (line.startsWith("## ")) {
      flushList();
      blocks.push(
        <h3 key={i} className="mt-1 text-base font-semibold">
          {line.slice(3)}
        </h3>
      );
    } else if (line.startsWith("- ")) {
      listItems.push(line.slice(2));
    } else if (line.length === 0) {
      flushList();
    } else {
      flushList();
      blocks.push(
        <p key={i} className="text-sm leading-relaxed">
          {line}
        </p>
      );
    }
  });
  flushList();
  return blocks;
}

export default function NewJob() {
  const navigate = useNavigate();

  const [title, setTitle] = useState("");
  const [level, setLevel] = useState("");
  const [mustHaves, setMustHaves] = useState("");
  const [niceToHaves, setNiceToHaves] = useState("");
  const [location, setLocation] = useState("");
  const [compMin, setCompMin] = useState("");
  const [compMax, setCompMax] = useState("");

  const [result, setResult] = useState<CreateJobResponse | null>(null);

  const mutation = useMutation({
    mutationFn: (draft: JobDraft) => api.createJob(draft) as Promise<CreateJobResponse>,
    onSuccess: (data) => setResult(data),
  });

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setResult(null);
    const draft: JobDraft = {
      title: title.trim(),
      level: level.trim() || null,
      must_haves: parseList(mustHaves),
      nice_to_haves: parseList(niceToHaves),
      location: location.trim() || null,
      comp_min: compMin.trim() ? Number(compMin) : null,
      comp_max: compMax.trim() ? Number(compMax) : null,
    };
    mutation.mutate(draft);
  }

  const apiError = mutation.error as ApiError | null;
  const flags = result?.inclusive_language_flags ?? [];

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">New job</h1>
        <p className="text-sm text-[hsl(var(--muted-foreground))]">
          Fill in the basics — PeopleOps Copilot drafts the full job description and runs an
          inclusive-language check in a single pass.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Role details</CardTitle>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="flex flex-col gap-4">
            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Title</span>
              <input
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g. Senior Backend Engineer"
                className={FIELD_CLASSES}
              />
            </label>

            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Level</span>
              <input
                value={level}
                onChange={(e) => setLevel(e.target.value)}
                placeholder="e.g. senior"
                className={FIELD_CLASSES}
              />
            </label>

            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Must-haves</span>
              <input
                value={mustHaves}
                onChange={(e) => setMustHaves(e.target.value)}
                placeholder="Comma-separated, e.g. Python, Postgres"
                className={FIELD_CLASSES}
              />
            </label>

            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Nice-to-haves</span>
              <input
                value={niceToHaves}
                onChange={(e) => setNiceToHaves(e.target.value)}
                placeholder="Comma-separated, e.g. Kubernetes"
                className={FIELD_CLASSES}
              />
            </label>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <label className="flex flex-col gap-1 text-sm">
                <span className="font-medium">Location</span>
                <input
                  value={location}
                  onChange={(e) => setLocation(e.target.value)}
                  placeholder="e.g. Bengaluru"
                  className={FIELD_CLASSES}
                />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="font-medium">Comp band — min</span>
                <input
                  type="number"
                  inputMode="numeric"
                  value={compMin}
                  onChange={(e) => setCompMin(e.target.value)}
                  placeholder="e.g. 150000"
                  className={FIELD_CLASSES}
                />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="font-medium">Comp band — max</span>
                <input
                  type="number"
                  inputMode="numeric"
                  value={compMax}
                  onChange={(e) => setCompMax(e.target.value)}
                  placeholder="e.g. 190000"
                  className={FIELD_CLASSES}
                />
              </label>
            </div>
          </CardContent>
          <CardFooter className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
            {apiError ? (
              <ErrorBanner code={apiError.code} message={apiError.message} className="flex-1" />
            ) : (
              <span />
            )}
            <Button type="submit" isLoading={mutation.isPending} disabled={!title.trim()}>
              Generate job description
            </Button>
          </CardFooter>
        </form>
      </Card>

      {mutation.isPending && (
        <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
          <Spinner size="sm" /> Drafting the job description…
        </div>
      )}

      {result && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle>{result.job.title}</CardTitle>
            <Badge variant="success">Saved</Badge>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {/* Inclusive-language pass — demo step 5: this must render
                visibly, with the suggested rewrite next to each flag. */}
            {flags.length > 0 ? (
              <div
                className="flex flex-col gap-3 rounded-md border p-3"
                style={{ borderColor: "hsl(var(--warning))", backgroundColor: "hsl(var(--warning) / 0.08)" }}
              >
                <p className="text-sm font-semibold" style={{ color: "hsl(var(--warning))" }}>
                  Inclusive-language check flagged {flags.length} phrase{flags.length === 1 ? "" : "s"}
                </p>
                <ul className="flex flex-col gap-3">
                  {flags.map((flag, i) => (
                    <li key={i} className="text-sm">
                      <div>
                        <span className="rounded bg-[hsl(var(--muted))] px-1.5 py-0.5 font-mono text-xs">
                          {flag.phrase}
                        </span>
                      </div>
                      <p className="mt-1 text-[hsl(var(--muted-foreground))]">{flag.reason}</p>
                      <p className="mt-1">
                        Suggested rewrite: <span className="font-medium">{flag.suggestion}</span>
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <Badge variant="success">No inclusive-language issues flagged</Badge>
            )}

            <div className="flex flex-col gap-1 rounded-md border border-[hsl(var(--border))] p-4">
              {renderJdMarkdown(result.job.description_md ?? "")}
            </div>
          </CardContent>
          <CardFooter className="justify-end gap-2">
            <Button variant="outline" onClick={() => setResult(null)}>
              Create another
            </Button>
            <Button onClick={() => navigate(`/jobs/${result.job.id}`)}>View job</Button>
          </CardFooter>
        </Card>
      )}
    </div>
  );
}
