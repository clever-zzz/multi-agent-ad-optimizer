import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export type BadgeTone =
  | "neutral"
  | "brand"
  | "positive"
  | "negative"
  | "warning"
  | "info"
  | "violet";

const TONES: Record<BadgeTone, string> = {
  neutral: "bg-surface-3 text-ink-2 border-line-strong",
  brand: "bg-brand-600/15 text-brand-300 border-brand-600/40",
  positive: "bg-pos/12 text-pos border-pos/35",
  negative: "bg-neg/12 text-neg border-neg/35",
  warning: "bg-warn/12 text-warn border-warn/35",
  info: "bg-accent-400/12 text-accent-400 border-accent-400/35",
  violet: "bg-violet/12 text-violet border-violet/35",
};

export interface BadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
  dot?: boolean;
  title?: string;
}

export function Badge({ tone = "neutral", children, className, dot = false, title }: BadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5",
        "text-[11px] font-medium leading-4 tracking-wide uppercase whitespace-nowrap",
        TONES[tone],
        className,
      )}
    >
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}