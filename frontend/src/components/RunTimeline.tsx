import { useI18n } from "@/i18n";
import { useEffect, useRef } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "@/components/ui/Icon";
import { formatNumber, formatTime, humanize } from "@/lib/format";
import type { TimelineItem } from "@/lib/timeline";

const AGENT_COLOR: Record<string, string> = {
  supervisor: "var(--color-violet)",
  monitor: "var(--color-accent-400)",
  audience: "var(--color-brand-400)",
  creative: "var(--color-warn)",
  bidding: "var(--color-pos)",
  optimize: "#f472b6",
};

const AGENT_ICON: Record<string, IconName> = {
  supervisor: "layers",
  monitor: "eye",
  audience: "users",
  creative: "sparkles",
  bidding: "wallet",
  optimize: "trendUp",
};

const TYPE_LABEL: Record<string, string> = {
  "run.started": "Run started",
  "run.succeeded": "Run succeeded",
  "run.failed": "Run failed",
  "run.cancelled": "Run cancelled",
  "agent.started": "Started",
  "agent.completed": "Completed",
  "agent.failed": "Failed",
};

function isFailure(type: string): boolean {
  return type.endsWith(".failed");
}

function isTerminal(type: string): boolean {
  return type.startsWith("run.");
}

// Renders the interesting parts of a payload without dumping raw JSON: agents
// publish a `_summary` block plus narrative lines, and both are shown as chips
// and sentences respectively.
function SummaryChips({ payload }: { payload: Record<string, unknown> }) {
  const summary = payload.summary;
  if (!summary || typeof summary !== "object") return null;

  const entries = Object.entries(summary as Record<string, unknown>).filter(
    ([, value]) => typeof value === "number" || typeof value === "string" || typeof value === "boolean",
  );
  if (entries.length === 0) return null;

  return (
    <ul className="mt-1.5 flex flex-wrap gap-1.5">
      {entries.map(([key, value]) => (
        <li
          key={key}
          className="tnum inline-flex items-center gap-1 rounded-md border border-line bg-surface-2 px-1.5 py-0.5 text-[11px]"
        >
          <span className="text-ink-3">{humanize(key)}</span>
          <span className="font-semibold text-ink-1">
            {typeof value === "number"
              ? Number.isInteger(value)
                ? formatNumber(value)
                : value.toFixed(2)
              : String(value)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Narrative({ payload }: { payload: Record<string, unknown> }) {
  const messages = payload.messages;
  if (!Array.isArray(messages) || messages.length === 0) return null;
  return (
    <div className="mt-1.5 flex flex-col gap-1">
      {messages.map((message, index) => (
        <p key={index} className="text-[13px] leading-relaxed text-ink-2">
          {String(message)}
        </p>
      ))}
    </div>
  );
}

function ErrorText({ payload }: { payload: Record<string, unknown> }) {
  const error = payload.error;
  if (!error) return null;
  return (
    <p className="mt-1 font-mono text-xs break-words text-neg">
      {String(error)}
      {payload.error_type ? ` (${String(payload.error_type)})` : ""}
    </p>
  );
}

export interface RunTimelineProps {
  items: TimelineItem[];
  live?: boolean;
  autoScroll?: boolean;
  className?: string;
  emptyLabel?: string;
}

export function RunTimeline({
  items,
  live = false,
  autoScroll = true,
  className,
  emptyLabel = "No events yet",
}: RunTimelineProps) {
  const { t } = useI18n();
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoScroll && live) endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [items.length, autoScroll, live]);

  if (items.length === 0) {
    return (
      <div className={cn("grid place-items-center rounded-lg border border-dashed border-line px-4 py-12", className)}>
        <p className="text-xs text-ink-3">{t(emptyLabel)}</p>
      </div>
    );
  }

  return (
    <ol className={cn("relative flex flex-col gap-0", className)}>
      {items.map((item, index) => {
        const color = AGENT_COLOR[item.agent] ?? "var(--color-line-strong)";
        const icon = AGENT_ICON[item.agent] ?? "activity";
        const failed = isFailure(item.type);
        const terminal = isTerminal(item.type);
        const isLast = index === items.length - 1;
        const duration =
          typeof item.payload.duration_ms === "number" ? item.payload.duration_ms : null;

        return (
          <li key={item.id || `${item.seq}-${index}`} className="relative flex gap-3 pl-1">
            <div className="relative flex w-6 shrink-0 flex-col items-center">
              <span
                className={cn(
                  "z-10 grid size-6 place-items-center rounded-full border bg-surface-1",
                  failed ? "border-neg/50 text-neg" : "border-line-strong",
                  terminal && !failed && "border-pos/50 text-pos",
                )}
                style={{ color: failed || terminal ? undefined : color }}
              >
                <Icon name={failed ? "warning" : icon} size={12} />
              </span>
              {!isLast && <span className="w-px flex-1 bg-line" />}
            </div>

            <div className={cn("min-w-0 flex-1", isLast ? "pb-1" : "pb-4")}>
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="text-[13px] font-semibold text-ink-1 capitalize">
                  {item.agent === "supervisor" ? t("Supervisor") : humanize(item.agent)}
                </span>
                <span
                  className={cn(
                    "text-xs",
                    failed ? "font-medium text-neg" : "text-ink-3",
                  )}
                >
                  {t(TYPE_LABEL[item.type] ?? item.type)}
                </span>
                {typeof item.payload.iteration === "number" && item.payload.iteration > 0 && (
                  <span className="tnum rounded border border-line bg-surface-2 px-1 text-[10px] text-ink-3">
                    {t("iter {n}", { n: item.payload.iteration })}
                  </span>
                )}
                {duration !== null && (
                  <span className="tnum text-[11px] text-ink-3">{duration.toFixed(0)} ms</span>
                )}
                <span className="tnum ml-auto shrink-0 text-[11px] text-ink-3">
                  {item.createdAt ? formatTime(item.createdAt) : `#${item.seq}`}
                </span>
              </div>

              <Narrative payload={item.payload} />
              <SummaryChips payload={item.payload} />
              <ErrorText payload={item.payload} />
            </div>
          </li>
        );
      })}
      <div ref={endRef} />
    </ol>
  );
}