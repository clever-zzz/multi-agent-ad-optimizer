import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "@/components/ui/Icon";
import { Skeleton } from "@/components/ui/Skeleton";

export type TrendDirection = "up" | "down" | "flat";

export interface StatCardProps {
  label: string;
  value: ReactNode;
  unit?: ReactNode;
  icon?: IconName;
  trend?: { direction: TrendDirection; label: string };
  hint?: ReactNode;
  tone?: "neutral" | "positive" | "negative" | "warning" | "brand" | "violet";
  loading?: boolean;
  className?: string;
}

const TONE_ACCENT = {
  neutral: "text-ink-2 bg-surface-3 border-line-strong",
  positive: "text-pos bg-pos/10 border-pos/25",
  negative: "text-neg bg-neg/10 border-neg/25",
  warning: "text-warn bg-warn/10 border-warn/25",
  brand: "text-brand-300 bg-brand-600/12 border-brand-600/30",
  violet: "text-violet bg-violet/10 border-violet/25",
} as const;

const TREND_STYLE: Record<TrendDirection, { className: string; icon: IconName }> = {
  up: { className: "text-pos", icon: "arrowUp" },
  down: { className: "text-neg", icon: "arrowDown" },
  flat: { className: "text-ink-3", icon: "arrowUp" },
};

export function StatCard({
  label,
  value,
  unit,
  icon,
  trend,
  hint,
  tone = "neutral",
  loading = false,
  className,
}: StatCardProps) {
  return (
    <div className={cn("card relative overflow-hidden p-4", className)}>
      <div className="flex items-start justify-between gap-3">
        <p className="text-[11px] font-semibold tracking-[0.1em] text-ink-3 uppercase">{label}</p>
        {icon && (
          <span className={cn("grid size-7 shrink-0 place-items-center rounded-lg border", TONE_ACCENT[tone])}>
            <Icon name={icon} size={14} />
          </span>
        )}
      </div>

      {loading ? (
        <Skeleton className="mt-3 h-7 w-24" />
      ) : (
        <p className="tnum mt-2 flex items-baseline gap-1 text-2xl font-semibold tracking-tight text-ink-1">
          {value}
          {unit && <span className="text-xs font-medium text-ink-3">{unit}</span>}
        </p>
      )}

      <div className="mt-2 flex items-center gap-2">
        {trend && !loading && (
          <span className={cn("inline-flex items-center gap-1 text-xs font-medium", TREND_STYLE[trend.direction].className)}>
            {trend.direction !== "flat" && <Icon name={TREND_STYLE[trend.direction].icon} size={12} />}
            {trend.label}
          </span>
        )}
        {hint && !loading && <span className="truncate text-xs text-ink-3">{hint}</span>}
      </div>
    </div>
  );
}