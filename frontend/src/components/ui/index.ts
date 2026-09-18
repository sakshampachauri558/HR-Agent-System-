// Barrel export for shared UI primitives. Every page/feature agent should
// import from here (`from "../../components/ui"`) instead of inventing its
// own button/card/badge/spinner/empty-state/error-banner, so the app reads
// as one product.
export { Button } from "./Button";
export type { ButtonProps, ButtonVariant, ButtonSize } from "./Button";

export { Card, CardHeader, CardTitle, CardContent, CardFooter } from "./Card";
export type { CardProps } from "./Card";

export { Badge } from "./Badge";
export type { BadgeProps, BadgeVariant } from "./Badge";

export { Spinner } from "./Spinner";
export type { SpinnerProps, SpinnerSize } from "./Spinner";

export { EmptyState } from "./EmptyState";
export type { EmptyStateProps } from "./EmptyState";

export { ErrorBanner } from "./ErrorBanner";
export type { ErrorBannerProps } from "./ErrorBanner";
