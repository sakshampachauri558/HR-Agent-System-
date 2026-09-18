import React, { useState } from "react";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "./ui";

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

/**
 * Shape returned by GET /api/health. Kept as a local, loosely-typed
 * interface (not imported from types.ts) so the shell never breaks while
 * A0's contract file is still being written — every field is optional
 * except `status`/`db`/`llm_provider`, which app/main.py always sends.
 */
interface HealthResponse {
  status: string;
  db: string;
  llm_provider: string;
  llm_model?: string | null;
}

interface NavItem {
  label: string;
  to: string;
}

// The five directly-navigable destinations. /jobs/:id (Job Board) and
// /eval/:id (Evaluation Detail) are the other two routes in the PRD §9
// table -- they're reached contextually (Jobs list -> row -> Job Board ->
// candidate -> Evaluation Detail), exactly the flow in the §14 demo script,
// rather than as static sidebar links, since they require a resource id
// the sidebar doesn't have.
const NAV_ITEMS: NavItem[] = [
  { label: "Policy Chat", to: "/chat" },
  { label: "Jobs", to: "/jobs" },
  { label: "New Job", to: "/jobs/new" },
  { label: "Analytics", to: "/analytics" },
  { label: "Admin", to: "/admin" },
];

async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch("/api/health");
  if (!res.ok) {
    throw new Error(`health check returned ${res.status}`);
  }
  return (await res.json()) as HealthResponse;
}

function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 15000,
    refetchOnWindowFocus: false,
    retry: 1,
  });
}

/**
 * Live backend + model status, polled every 15s. Shows which provider/model
 * is actually serving requests (real product signal worth surfacing) and a
 * small, proportionate indicator if the backend is unreachable or degraded
 * — an app that silently hides its own status is worse than one that
 * quietly admits it.
 */
function StatusDot({ tone }: { tone: "ok" | "warning" | "danger" | "neutral" }) {
  const color =
    tone === "ok"
      ? "bg-[hsl(var(--success))]"
      : tone === "warning"
        ? "bg-[hsl(var(--warning))]"
        : tone === "danger"
          ? "bg-[hsl(var(--danger))]"
          : "bg-[hsl(var(--muted-foreground))]";
  return <span aria-hidden="true" className={cn("inline-block h-2 w-2 shrink-0 rounded-full", color)} />;
}

function HealthStrip() {
  const { data, isError } = useHealth();

  if (isError) {
    return (
      <span className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
        <StatusDot tone="danger" />
        backend unreachable
      </span>
    );
  }

  if (!data) {
    return (
      <span className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
        <StatusDot tone="neutral" />
        checking…
      </span>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
        <StatusDot tone={data.status === "ok" ? "ok" : "warning"} />
        {data.status}
      </span>
      <Badge variant="neutral">
        {data.llm_provider}
        {data.llm_model ? ` · ${data.llm_model}` : ""}
      </Badge>
    </div>
  );
}

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-1">
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/jobs"}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "rounded-md px-3 py-2 text-sm font-medium transition-colors",
              isActive
                ? "bg-[hsl(var(--accent))] text-[hsl(var(--accent-foreground))]"
                : "text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]"
            )
          }
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

export interface ShellProps {
  children: React.ReactNode;
}

/**
 * Persistent app layout: sidebar nav (collapses to a mobile top bar under
 * 768px) + a header strip showing live backend health. Every page renders
 * inside this shell via <Shell><Routes>...</Routes></Shell> in App.tsx.
 */
export function Shell({ children }: ShellProps) {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="flex h-screen w-full flex-col bg-[hsl(var(--background))] text-[hsl(var(--foreground))] md:flex-row">
      {/* Mobile top bar (< 768px) */}
      <header className="flex items-center justify-between border-b border-[hsl(var(--border))] p-3 md:hidden">
        <span className="text-base font-semibold">PeopleOps Copilot</span>
        <button
          type="button"
          aria-label="Toggle navigation"
          onClick={() => setMobileOpen((open) => !open)}
          className="rounded-md border border-[hsl(var(--border))] px-3 py-1.5 text-sm"
        >
          Menu
        </button>
      </header>

      {mobileOpen && (
        <div className="border-b border-[hsl(var(--border))] p-3 md:hidden">
          <NavLinks onNavigate={() => setMobileOpen(false)} />
        </div>
      )}

      {/* Desktop sidebar (>= 768px) */}
      <aside className="hidden w-60 shrink-0 flex-col gap-6 border-r border-[hsl(var(--border))] p-4 md:flex">
        <span className="text-lg font-semibold">PeopleOps Copilot</span>
        <NavLinks />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Desktop health header */}
        <header className="hidden items-center justify-between gap-4 border-b border-[hsl(var(--border))] px-6 py-3 md:flex">
          <span className="text-sm font-medium text-[hsl(var(--muted-foreground))]">System status</span>
          <HealthStrip />
        </header>
        {/* Mobile health strip */}
        <div className="border-b border-[hsl(var(--border))] px-3 py-2 md:hidden">
          <HealthStrip />
        </div>

        <main className="flex-1 overflow-y-auto p-4 md:p-6">{children}</main>
      </div>
    </div>
  );
}
