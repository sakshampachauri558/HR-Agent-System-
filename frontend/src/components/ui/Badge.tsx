import React from "react";

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

export type BadgeVariant = "neutral" | "success" | "warning" | "danger" | "info";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  neutral: "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]",
  success: "bg-[hsl(var(--success))] text-white",
  warning: "bg-[hsl(var(--warning))] text-black",
  danger: "bg-[hsl(var(--danger))] text-white",
  info: "bg-[hsl(var(--info))] text-white",
};

/**
 * import { Badge } from "../../components/ui";
 * <Badge variant="success">Scored</Badge>
 */
export function Badge({ className, variant = "neutral", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium",
        VARIANT_CLASSES[variant],
        className
      )}
      {...props}
    />
  );
}
