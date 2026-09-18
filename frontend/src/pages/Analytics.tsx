/**
 * HR Analytics — PRD §4 F6 / demo step 9.
 *
 * Four live charts built from `GET /api/analytics` (pipeline funnel,
 * fit-score histogram, matched-skills vs. gaps, and LLM usage by
 * provider), plus the natural-language query box (`POST
 * /api/analytics/query`) that renders the exact SQL that ran — the
 * feature's trust story, since this is the one endpoint in the app where
 * model output could reach the database. See
 * `backend/app/routers/analytics.py`'s module docstring for the full
 * treatment: typed query plan (never a SQL string), a hardcoded
 * table/column allowlist, bound parameters only, the read-only
 * `analytics_ro` Postgres role, and an always-applied LIMIT.
 *
 * Owned by A9 (Analytics). Talks only to `api.getAnalytics` /
 * `api.queryAnalytics`.
 */
import React, { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, ApiError } from "../api";
import type { AnalyticsResponse, ResumeStatus, UsageStat } from "../types";
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

// ---------------------------------------------------------------------------
// Chart colors — drawn from index.css's CSS variables so bars flip with
// light/dark mode exactly like the rest of the app. Every chart here uses
// a single hue for an ordered/magnitude axis (status, score bucket,
// provider) rather than a multi-hue categorical set: this app's 5-token
// palette (accent/success/warning/danger/info) was checked against the
// dataviz skill's colorblind-separation validator and does NOT pass as a
// simultaneous categorical set at these exact hues (the warning/success
// pair falls below the safe separation floor). Rather than invent a new,
// unvalidated palette outside this app's design system, "matched skills"
// vs. "gaps" are rendered as two separately headed panels — never
// adjacent bars sharing one plot — so identity comes from the label, not
// from telling two hues apart.
// ---------------------------------------------------------------------------
const ACCENT = "hsl(var(--accent))";
const SUCCESS = "hsl(var(--success))";
const WARNING = "hsl(var(--warning))";
const MUTED_FG = "hsl(var(--muted-foreground))";
const BORDER = "hsl(var(--border))";
const AXIS_TICK = { fill: MUTED_FG, fontSize: 11 };

// ---------------------------------------------------------------------------
// Local, additive types. `GET /api/analytics` returns the frozen
// `AnalyticsResponse` shape (pipeline/score_distribution/skill_gaps/usage)
// plus `budget_used_today` / `budget_limit` (the same additive pattern
// `/api/health` and A6's `routers/jobs.py` use) and a `billable` flag per
// usage row — see `routers/analytics.py`'s `get_analytics` / `_usage`.
// ---------------------------------------------------------------------------
interface UsageStatWithBilling extends UsageStat {
  billable?: boolean;
}

interface AnalyticsResponseWithBudget extends Omit<AnalyticsResponse, "usage"> {
  usage: UsageStatWithBilling[];
  budget_used_today?: number | null;
  budget_limit?: number | null;
}

const STATUS_LABEL: Record<ResumeStatus, string> = {
  queued: "Queued",
  parsing: "Parsing",
  evaluating: "Evaluating",
  scored: "Scored",
  failed: "Failed",
};

// ---------------------------------------------------------------------------
// Shared chart chrome
// ---------------------------------------------------------------------------

function ChartCard({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description && <p className="text-xs text-[hsl(var(--muted-foreground))]">{description}</p>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

interface ChartTooltipProps {
  active?: boolean;
  label?: string | number;
  payload?: Array<{ value?: number | string; name?: string | number; dataKey?: string | number }>;
}

function ChartTooltip({ active, payload, label }: ChartTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-3 py-2 text-xs shadow-md">
      <p className="mb-1 font-medium text-[hsl(var(--foreground))]">{label}</p>
      {payload.map((p, i) => (
        <p key={`${p.dataKey ?? i}`} className="text-[hsl(var(--foreground))]">
          <span className="font-semibold">{p.value}</span>{" "}
          <span className="text-[hsl(var(--muted-foreground))]">{p.name ?? "value"}</span>
        </p>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chart 1 — pipeline funnel
// ---------------------------------------------------------------------------

function PipelineChart({ data }: { data: AnalyticsResponse["pipeline"] }) {
  const chartData = data.map((d) => ({ status: STATUS_LABEL[d.status] ?? d.status, count: d.count }));
  const total = chartData.reduce((sum, d) => sum + d.count, 0);
  if (total === 0) {
    return (
      <EmptyState
        title="No resumes yet"
        description="Upload resumes against a job to populate the pipeline."
      />
    );
  }
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={chartData} margin={{ top: 8, right: 8, left: -16, bottom: 0 }} barCategoryGap="24%">
        <CartesianGrid stroke={BORDER} vertical={false} />
        <XAxis dataKey="status" tick={AXIS_TICK} axisLine={{ stroke: BORDER }} tickLine={false} />
        <YAxis allowDecimals={false} tick={AXIS_TICK} axisLine={false} tickLine={false} width={32} />
        <Tooltip content={<ChartTooltip />} cursor={{ fill: "hsl(var(--muted))" }} />
        <Bar dataKey="count" name="resumes" fill={ACCENT} radius={[4, 4, 0, 0]} maxBarSize={40} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Chart 2 — fit-score distribution
// ---------------------------------------------------------------------------

function ScoreDistributionChart({ data }: { data: AnalyticsResponse["score_distribution"] }) {
  const total = data.reduce((sum, d) => sum + d.count, 0);
  if (total === 0) {
    return (
      <EmptyState
        title="No scored candidates yet"
        description="Evaluate a resume against a job to populate this histogram."
      />
    );
  }
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={[...data]} margin={{ top: 8, right: 8, left: -16, bottom: 0 }} barCategoryGap="24%">
        <CartesianGrid stroke={BORDER} vertical={false} />
        <XAxis dataKey="range_label" tick={AXIS_TICK} axisLine={{ stroke: BORDER }} tickLine={false} />
        <YAxis allowDecimals={false} tick={AXIS_TICK} axisLine={false} tickLine={false} width={32} />
        <Tooltip content={<ChartTooltip />} cursor={{ fill: "hsl(var(--muted))" }} />
        <Bar dataKey="count" name="candidates" fill={ACCENT} radius={[4, 4, 0, 0]} maxBarSize={40} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Chart 3 — skills: matched vs. gaps (two labeled, monochrome mini-lists)
// ---------------------------------------------------------------------------

function SkillBarList({ items, color }: { items: { skill: string; count: number }[]; color: string }) {
  if (items.length === 0) {
    return <p className="text-xs text-[hsl(var(--muted-foreground))]">Nothing to show yet.</p>;
  }
  const max = Math.max(...items.map((i) => i.count));
  return (
    <ul className="flex flex-col gap-1.5">
      {items.map((item) => (
        <li key={item.skill} className="flex items-center gap-2 text-xs">
          <span className="w-28 shrink-0 truncate text-[hsl(var(--foreground))]" title={item.skill}>
            {item.skill}
          </span>
          <span className="h-2 flex-1 overflow-hidden rounded-full bg-[hsl(var(--muted))]">
            <span
              className="block h-full rounded-full"
              style={{ width: `${Math.max(6, (item.count / max) * 100)}%`, backgroundColor: color }}
            />
          </span>
          <span className="w-5 shrink-0 text-right font-medium text-[hsl(var(--foreground))]">{item.count}</span>
        </li>
      ))}
    </ul>
  );
}

function SkillGapsChart({ data }: { data: AnalyticsResponse["skill_gaps"] }) {
  if (data.top_present.length === 0 && data.top_missing.length === 0) {
    return (
      <EmptyState
        title="No evaluations yet"
        description="Evaluate candidates to see matched skills and gaps across the pool."
      />
    );
  }
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <div>
        <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          Top matched skills
        </p>
        <SkillBarList items={data.top_present} color={SUCCESS} />
      </div>
      <div>
        <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          Top gaps
        </p>
        <SkillBarList items={data.top_missing} color={WARNING} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chart 4 — LLM usage / spend, by provider
// ---------------------------------------------------------------------------

function UsagePanel({
  usage,
  budgetUsed,
  budgetLimit,
}: {
  usage: UsageStatWithBilling[];
  budgetUsed: number | null | undefined;
  budgetLimit: number | null | undefined;
}) {
  if (usage.length === 0) {
    return (
      <EmptyState
        title="No LLM calls yet"
        description="Ask a policy question or evaluate a resume to populate usage."
      />
    );
  }
  const chartData = usage.map((u) => ({ provider: u.provider, requests: u.request_count }));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="success">Total inference spend: $0</Badge>
        {budgetUsed != null && budgetLimit != null && (
          <Badge variant={budgetLimit - budgetUsed <= 5 ? "danger" : "neutral"}>
            {budgetUsed}/{budgetLimit} billed requests today
          </Badge>
        )}
      </div>

      <ResponsiveContainer width="100%" height={Math.max(120, chartData.length * 40)}>
        <BarChart data={chartData} layout="vertical" margin={{ top: 0, right: 16, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={BORDER} horizontal={false} />
          <XAxis type="number" allowDecimals={false} tick={AXIS_TICK} axisLine={false} tickLine={false} />
          <YAxis
            type="category"
            dataKey="provider"
            tick={AXIS_TICK}
            axisLine={{ stroke: BORDER }}
            tickLine={false}
            width={70}
          />
          <Tooltip content={<ChartTooltip />} cursor={{ fill: "hsl(var(--muted))" }} />
          <Bar dataKey="requests" name="requests" fill={ACCENT} radius={[0, 4, 4, 0]} maxBarSize={24} />
        </BarChart>
      </ResponsiveContainer>

      <div className="flex flex-col divide-y divide-[hsl(var(--border))] overflow-hidden rounded-md border border-[hsl(var(--border))]">
        <div className="grid grid-cols-5 gap-2 bg-[hsl(var(--muted))] px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          <span>Provider</span>
          <span>Requests</span>
          <span>Avg latency</span>
          <span>Tokens (in/out)</span>
          <span>Billing</span>
        </div>
        {usage.map((u) => (
          <div key={`${u.provider}-${u.model}`} className="grid grid-cols-5 items-center gap-2 px-3 py-2 text-xs">
            <span className="truncate font-medium text-[hsl(var(--foreground))]" title={u.model}>
              {u.provider}
            </span>
            <span>{u.request_count}</span>
            <span>{Math.round(u.avg_latency_ms)} ms</span>
            <span>
              {u.total_input_tokens.toLocaleString()} / {u.total_output_tokens.toLocaleString()}
            </span>
            <span>
              {u.billable === false ? (
                <Badge variant="neutral">unbilled</Badge>
              ) : (
                <Badge variant="warning">billed</Badge>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Natural-language query box — POST /api/analytics/query
// ---------------------------------------------------------------------------

const EXAMPLE_QUESTIONS = ["How many candidates scored above 60?", "Break down evaluations by recommendation"];

function QueryResultTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (rows.length === 0) {
    return <p className="text-xs text-[hsl(var(--muted-foreground))]">Query returned no rows.</p>;
  }
  const columns = Object.keys(rows[0]);
  return (
    <div className="overflow-x-auto rounded-md border border-[hsl(var(--border))]">
      <table className="w-full text-left text-xs">
        <thead className="bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]">
          <tr>
            {columns.map((col) => (
              <th key={col} className="px-3 py-1.5 font-semibold">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[hsl(var(--border))]">
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((col) => (
                <td key={col} className="px-3 py-1.5 text-[hsl(var(--foreground))]">
                  {String(row[col] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NlQueryBox() {
  const [question, setQuestion] = useState("");
  const mutation = useMutation({
    mutationFn: (q: string) => api.queryAnalytics({ question: q }),
  });

  function ask(q: string) {
    const trimmed = q.trim();
    if (!trimmed || mutation.isPending) return;
    mutation.mutate(trimmed);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    ask(question);
  }

  const apiError = mutation.error instanceof ApiError ? mutation.error : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Ask the data</CardTitle>
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          The model never writes SQL — it returns a typed table/column/filter plan, checked
          against a hardcoded allowlist, with bound parameters and a limit applied before this
          runs read-only as the <code>analytics_ro</code> Postgres role. The SQL below is
          exactly, and only, what ran.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form onSubmit={handleSubmit} className="flex flex-col gap-2 sm:flex-row">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. How many candidates scored above 60?"
            disabled={mutation.isPending}
            className="flex-1 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm text-[hsl(var(--foreground))] outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--accent))]"
          />
          <Button type="submit" isLoading={mutation.isPending} disabled={!question.trim()}>
            Ask
          </Button>
        </form>

        <div className="flex flex-wrap gap-2">
          {EXAMPLE_QUESTIONS.map((q) => (
            <Button
              key={q}
              variant="outline"
              size="sm"
              type="button"
              onClick={() => ask(q)}
              disabled={mutation.isPending}
            >
              {q}
            </Button>
          ))}
        </div>

        {mutation.isPending && (
          <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
            <Spinner size="sm" label="Querying" /> Composing an allowlisted query…
          </div>
        )}

        {mutation.isError && (
          <ErrorBanner
            code={apiError?.code ?? "unknown_error"}
            message={apiError?.message ?? "That question couldn't be answered safely."}
          />
        )}

        {mutation.isSuccess && mutation.data && (
          <div className="flex flex-col gap-3">
            <div>
              <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
                SQL executed (read-only, as analytics_ro)
              </p>
              <pre className="overflow-x-auto rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))] p-3 text-xs text-[hsl(var(--foreground))]">
                <code>{mutation.data.sql}</code>
              </pre>
            </div>
            <div>
              <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
                Result
              </p>
              <QueryResultTable rows={mutation.data.rows} />
            </div>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">{mutation.data.explanation}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Analytics() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["analytics"],
    queryFn: () => api.getAnalytics() as Promise<AnalyticsResponseWithBudget>,
  });

  const apiError = error as ApiError | null;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">Analytics</h1>
        <p className="text-sm text-[hsl(var(--muted-foreground))]">
          Live charts over local pipeline and evaluation data, plus a natural-language query
          box that shows exactly the SQL it ran.
        </p>
      </div>

      {isLoading && (
        <div className="flex items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
          <Spinner size="sm" /> Loading analytics…
        </div>
      )}

      {isError && (
        <ErrorBanner
          code={apiError?.code ?? "unknown_error"}
          message={apiError?.message ?? "Failed to load analytics."}
          onRetry={() => refetch()}
        />
      )}

      {data && (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <ChartCard title="Pipeline" description="Resumes by current status.">
              <PipelineChart data={data.pipeline} />
            </ChartCard>
            <ChartCard title="Fit-score distribution" description="Evaluated candidates by fixed score bucket.">
              <ScoreDistributionChart data={data.score_distribution} />
            </ChartCard>
            <ChartCard title="Skills: matched vs. gaps" description="Top 10 across every evaluation in the pool.">
              <SkillGapsChart data={data.skill_gaps} />
            </ChartCard>
            <ChartCard title="LLM usage by provider" description="Requests, latency, and tokens from the audit log.">
              <UsagePanel usage={data.usage} budgetUsed={data.budget_used_today} budgetLimit={data.budget_limit} />
            </ChartCard>
          </div>

          <NlQueryBox />
        </>
      )}
    </div>
  );
}
