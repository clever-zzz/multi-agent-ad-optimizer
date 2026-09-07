import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "@/components/ui/Icon";

export interface EmptyStateProps {
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
  icon?: IconName;
  tone?: "neutral" | "negative";
  className?: string;
}

export function EmptyState({
  title,
  hint,
  action,
  icon = "layers",
  tone = "neutral",
  className,
}: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 px-6 py-12 text-center", className)}>
      <span
        className={cn(
          "grid size-10 place-items-center rounded-full border",
          tone === "negative"
            ? "border-neg/30 bg-neg/10 text-neg"
            : "border-line-strong bg-surface-2 text-ink-3",
        )}
      >
        <Icon name={icon} size={18} />
      </span>
      <p className="text-sm font-medium text-ink-1">{title}</p>
      {hint && <p className="max-w-md text-xs text-ink-3">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}