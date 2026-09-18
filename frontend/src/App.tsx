import React, { Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { Shell } from "./components/Shell";
import { Button, EmptyState, Spinner } from "./components/ui";

/**
 * Lazy page registry.
 *
 * Wave-1/2 agents each own one file below and must:
 *   - create it at exactly this path
 *   - give it a default export: `export default function Chat() { ... }`
 *
 * Nothing else in this file needs to change when a page lands -- the import
 * just starts resolving instead of 404ing. Owners:
 *   pages/Chat.tsx        A4  (RAG Query)
 *   pages/Jobs.tsx        A6  (Jobs/JD)
 *   pages/NewJob.tsx      A6  (Jobs/JD)
 *   pages/JobBoard.tsx    A8  (Board + Kit)
 *   pages/EvalDetail.tsx  A8  (Board + Kit)
 *   pages/Analytics.tsx   A9  (Analytics)
 *   pages/Admin.tsx       A3  (RAG Ingest)
 */
const LazyChat = React.lazy(() => import("./pages/Chat"));
const LazyJobs = React.lazy(() => import("./pages/Jobs"));
const LazyNewJob = React.lazy(() => import("./pages/NewJob"));
const LazyJobBoard = React.lazy(() => import("./pages/JobBoard"));
const LazyEvalDetail = React.lazy(() => import("./pages/EvalDetail"));
const LazyAnalytics = React.lazy(() => import("./pages/Analytics"));
const LazyAdmin = React.lazy(() => import("./pages/Admin"));

interface RouteErrorBoundaryProps {
  label: string;
  children: React.ReactNode;
}

interface RouteErrorBoundaryState {
  hasError: boolean;
}

/**
 * Catches a failed lazy-page import or a render-time error on a page and
 * shows an honest error state with a retry action, instead of taking down
 * the whole app.
 */
class RouteErrorBoundary extends React.Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  state: RouteErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): RouteErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: unknown): void {
    // eslint-disable-next-line no-console
    console.warn(`[shell] "${this.props.label}" page failed to load:`, error);
  }

  render(): React.ReactNode {
    if (this.state.hasError) {
      return (
        <EmptyState
          title="Couldn't load this page"
          description={`Something went wrong loading ${this.props.label}. This is usually temporary.`}
          action={
            <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
              Retry
            </Button>
          }
        />
      );
    }
    return this.props.children;
  }
}

function PageSpinner() {
  return (
    <div className="flex items-center justify-center p-16">
      <Spinner size="lg" label="Loading page" />
    </div>
  );
}

function LazyRoute({
  label,
  Component,
}: {
  label: string;
  Component: React.LazyExoticComponent<React.ComponentType>;
}) {
  return (
    <RouteErrorBoundary label={label}>
      <Suspense fallback={<PageSpinner />}>
        <Component />
      </Suspense>
    </RouteErrorBoundary>
  );
}

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route path="/chat" element={<LazyRoute label="Policy Chat" Component={LazyChat} />} />
        <Route path="/jobs" element={<LazyRoute label="Jobs" Component={LazyJobs} />} />
        <Route path="/jobs/new" element={<LazyRoute label="New Job" Component={LazyNewJob} />} />
        <Route path="/jobs/:id" element={<LazyRoute label="Job Board" Component={LazyJobBoard} />} />
        <Route path="/eval/:id" element={<LazyRoute label="Evaluation Detail" Component={LazyEvalDetail} />} />
        <Route path="/analytics" element={<LazyRoute label="Analytics" Component={LazyAnalytics} />} />
        <Route path="/admin" element={<LazyRoute label="Admin" Component={LazyAdmin} />} />
        <Route
          path="*"
          element={<EmptyState title="Not found" description="There's nothing at this address." />}
        />
      </Routes>
    </Shell>
  );
}
