import type { ReactNode } from "react";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/cn";
import { clamp, formatAxisNumber } from "@/components/charts/chartUtils";

export interface BarListItem {
  id: string;
  label: string;
  value: number;
  hint?: string;
  tone?: "positive" | "negative" | "brand" | "warning" | "neutral" | "violet";
  onClick?: () => void;
}

const TONE_COLOR: Record<NonNullable<BarListItem["tone"]>, string> = {
  positive: "var(--color-pos)",
  negative: "var(--color-neg)",
  brand: "var(--color-brand-500)",
  warning: "var(--color-warn)",
  neutral: "var(--color-line-strong)",
  // Reserved for agent-generated content so it reads differently from human input.
  violet: "var(--color-violet)",
};

export interface BarListProps {
  items: BarListItem[];
  format?: (value: number) => string;
  emptyLabel?: string;
  className?: string;
  dense?: boolean;
}

// Horizontal comparison list. Easier to read than a bar chart for ranked
// leaderboards, and it degrades gracefully at narrow widths.
export function BarList({
  items,
  format = (value) => formatAxisNumber(value),
  emptyLabel,
  className,
  dense = false,
}: BarListProps) {
  const { t } = useI18n();

  if (items.length === 0) {
    return (
      <p className={cn("px-1 py-8 text-center text-xs text-ink-3", className)}>
        {emptyLabel ?? t("Nothing to rank yet")}
      </p>
    );
  }

  const max = Math.max(...items.map((item) => Math.abs(item.value)), Number.EPSILON);

  return (
    <ul className={cn("flex flex-col", dense ? "gap-1.5" : "gap-2.5", className)}>
      {items.map((item) => {
        const pct = clamp((Math.abs(item.value) / max) * 100, 1.5, 100);
        const color = TONE_COLOR[item.tone ?? "brand"];

        const body: ReactNode = (
          <>
            <div className={cn("flex items-baseline justify-between gap-3", !dense && "mb-1")}>
              <span className="min-w-0 flex-1 truncate text-[13px] text-ink-1">{item.label}</span>
              <span className="tnum shrink-0 text-[13px] font-semibold text-ink-1">
                {format(item.value)}
              </span>
            </div>
            <div
              className={cn(
                "w-full overflow-hidden rounded-full bg-surface-3",
                dense ? "h-1" : "h-1.5",
              )}
            >
              <div
                className="h-full rounded-full transition-[width] duration-500"
                style={{ width: `${pct}%`, background: color }}
              />
            </div>
            {item.hint && <p className="mt-1 truncate text-[11px] text-ink-3">{item.hint}</p>}
          </>
        );

        return (
          <li key={item.id}>
            {item.onClick ? (
              <button
                type="button"
                onClick={item.onClick}
                className="block w-full cursor-pointer rounded-md px-1.5 py-1 text-left transition-colors hover:bg-surface-2/60"
              >
                {body}
              </button>
            ) : (
              <div className="px-1.5 py-1">{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}