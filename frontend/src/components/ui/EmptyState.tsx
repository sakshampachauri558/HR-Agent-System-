import React from "react";

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

export interface EmptyStateProps {
  title: string;
  description?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}

/**
 * import { EmptyState } from "../../components/ui";
 * <EmptyState title="No resumes yet" description="Drop a few PDFs to get started." />
 */
export function EmptyState({ title, description, icon, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-[hsl(var(--border))] p-10 text-center",
        className
      )}
    >
      {icon && <div className="text-[hsl(var(--muted-foreground))]">{icon}</div>}
      <p className="text-sm font-medium text-[hsl(var(--foreground))]">{title}</p>
      {description && (
        <p className="max-w-sm text-sm text-[hsl(var(--muted-foreground))]">{description}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
