import React from "react";
import { Button } from "./Button";

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

export interface ErrorBannerProps {
  message: string;
  code?: string;
  onRetry?: () => void;
  className?: string;
}

/**
 * Renders the PRD §9 error envelope ({"error": {"code","message"}}) in a
 * single line. Pass `code`/`message` straight from a caught API error.
 *
 * import { ErrorBanner } from "../../components/ui";
 * <ErrorBanner code={err.code} message={err.message} onRetry={refetch} />
 */
export function ErrorBanner({ message, code, onRetry, className }: ErrorBannerProps) {
  return (
    <div
      role="alert"
      style={{ backgroundColor: "hsl(var(--danger) / 0.08)" }}
      className={cn(
        "flex items-start justify-between gap-3 rounded-md border border-[hsl(var(--danger))] p-3 text-sm text-[hsl(var(--danger))]",
        className
      )}
    >
      <div>
        {code && <p className="font-mono text-xs opacity-80">{code}</p>}
        <p>{message}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}
