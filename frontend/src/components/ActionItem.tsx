import { useI18n } from "@/i18n";
import { Link } from "react-router-dom";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { StatusPill } from "@/components/ui/StatusPill";
import { formatDateTime, formatPercent, humanize, truncate } from "@/lib/format";
import type { ActionType, OptimizationAction } from "@/lib/types";

const ACTION_ICON: Partial<Record<ActionType, "wallet" | "trendUp" | "sparkles" | "stop" | "play" | "users" | "flask">> = {
  adjust_budget: "wallet",
  adjust_bid: "trendUp",
  pause_creative: "stop",
  resume_creative: "play",
  refresh_creative: "sparkles",
  pause_campaign: "stop",
  resume_campaign: "play",
  expand_audience: "users",
  start_ab_test: "flask",
  stop_ab_test: "flask",
};

// Actions that move money get a stronger visual weight than copy tweaks, because
// the cost of a wrong approval is not the same across types.
const HIGH_IMPACT: ReadonlySet<ActionType> = new Set([
  "adjust_budget",
  "adjust_bid",
  "pause_campaign",
]);

type ConfidenceTone = "positive" | "warning" | "negative";

function confidenceTone(confidence: number): ConfidenceTone {
  if (confidence >= 0.75) return "positive";
  if (confidence >= 0.5) return "warning";
  return "negative";
}

// Literal class names only: Tailwind discovers utilities by scanning source text,
// so a template-built class would silently produce no style.
const CONFIDENCE_TEXT: Record<ConfidenceTone, string> = {
  positive: "text-pos",
  warning: "text-warn",
  negative: "text-neg",
};

const CONFIDENCE_BAR: Record<ConfidenceTone, string> = {
  positive: "bg-pos",
  warning: "bg-warn",
  negative: "bg-neg",
};

export interface ActionItemProps {
  action: OptimizationAction;
  campaignName?: string;
  canApprove?: boolean;
  canExecute?: boolean;
  busy?: boolean;
  selected?: boolean;
  onSelect?: (selected: boolean) => void;
  onApprove?: (executeImmediately: boolean) => void;
  onReject?: () => void;
  onExecute?: () => void;
  className?: string;
}

export function ActionItem({
  action,
  campaignName,
  canApprove = false,
  canExecute = false,
  busy = false,
  selected = false,
  onSelect,
  onApprove,
  onReject,
  onExecute,
  className,
}: ActionItemProps) {
  const { t } = useI18n();
  const id = action.id ?? "";
  const pending = action.status === "proposed";
  const approved = action.status === "approved";
  const tone = confidenceTone(action.confidence);
  const highImpact = HIGH_IMPACT.has(action.action_type);

  return (
    <article
      className={cn(
        "card flex flex-col gap-3 p-3.5 transition-colors",
        highImpact && pending && "border-warn/30",
        selected && "border-brand-500/60 bg-brand-600/5",
        className,
      )}
    >
      <div className="flex items-start gap-3">
        {onSelect && (
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => onSelect(event.target.checked)}
            aria-label={t("Select proposal {id}", { id })}
            disabled={!pending}
            className="mt-1 size-3.5 shrink-0 accent-[var(--color-brand-500)] disabled:opacity-40"
          />
        )}

        <span
          className={cn(
            "mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg border",
            highImpact
              ? "border-warn/30 bg-warn/10 text-warn"
              : "border-line-strong bg-surface-2 text-brand-300",
          )}
        >
          <Icon name={ACTION_ICON[action.action_type] ?? "checkSquare"} size={15} />
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h3 className="text-[13px] font-semibold text-ink-1">
              {humanize(action.action_type)}
            </h3>
            {highImpact && <Badge tone="warning">{t("budget impact")}</Badge>}
            <StatusPill domain="action" value={action.status} />
          </div>

          <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[11px] text-ink-3">
            {campaignName ? (
              <Link to={`/campaigns/${action.campaign_id}`} className="text-ink-2 hover:text-brand-300 hover:underline">
                {truncate(campaignName, 40)}
              </Link>
            ) : (
              <span className="font-mono">{truncate(action.campaign_id, 18)}</span>
            )}
            <span>·</span>
            <span>{t("proposed by {name}", { name: action.proposed_by })}</span>
            <span>·</span>
            <span>{formatDateTime(action.created_at)}</span>
          </p>
        </div>

        <div className="shrink-0 text-right">
          <p className={cn("tnum text-sm font-semibold", CONFIDENCE_TEXT[tone])}>
            {formatPercent(action.confidence, 0)}
          </p>
          <p className="text-[10px] tracking-wide text-ink-3 uppercase">{t("confidence")}</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2">
        <span className="tnum font-mono text-[13px] text-ink-3 line-through decoration-neg/60">
          {action.before_value || "—"}
        </span>
        <Icon name="chevronRight" size={13} className="shrink-0 text-ink-3" />
        <span className="tnum font-mono text-[13px] font-semibold text-pos">
          {action.after_value || "—"}
        </span>
        <div className="ml-auto h-1.5 w-24 overflow-hidden rounded-full bg-surface-3">
          <div
            className={cn("h-full rounded-full", CONFIDENCE_BAR[tone])}
            style={{ width: `${Math.round(action.confidence * 100)}%` }}
          />
        </div>
      </div>

      {action.reason && (
        <p className="text-[13px] leading-relaxed text-ink-2">{action.reason}</p>
      )}

      {action.error_message && (
        <p className="flex items-start gap-1.5 rounded-md border border-neg/30 bg-neg/8 px-2.5 py-2 text-xs text-neg">
          <Icon name="warning" size={13} className="mt-0.5 shrink-0" />
          {action.error_message}
        </p>
      )}

      {(action.approved_by || action.executed_at || action.external_reference) && (
        <p className="tnum text-[11px] text-ink-3">
          {action.approved_by && <>{t("approved by {name}", { name: action.approved_by })} </>}
          {action.executed_at && <>{t("· executed {time}", { time: formatDateTime(action.executed_at) })} </>}
          {action.external_reference && <>{t("· ref {reference}", { reference: action.external_reference })}</>}
        </p>
      )}

      {(pending || approved) && (canApprove || canExecute) && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
          {pending && canApprove && onApprove && (
            <>
              <Button variant="success" icon="check" onClick={() => onApprove(false)} disabled={busy}>
                {t("Approve")}
              </Button>
              {canExecute && onExecute && (
                <Button variant="primary" icon="play" onClick={() => onApprove(true)} disabled={busy}>
                  {t("Approve & execute")}
                </Button>
              )}
              {onReject && (
                <Button variant="ghost" icon="close" onClick={onReject} disabled={busy} className="text-neg hover:bg-neg/10">
                  {t("Reject")}
                </Button>
              )}
            </>
          )}
          {approved && canExecute && onExecute && (
            <Button variant="primary" icon="play" onClick={onExecute} disabled={busy} loading={busy}>
              {t("Execute now")}
            </Button>
          )}
          {action.run_id && (
            <Link to={`/runs/${action.run_id}`} className="ml-auto">
              <Button size="xs" variant="ghost" iconRight="external">
                {t("Open run")}
              </Button>
            </Link>
          )}
        </div>
      )}
    </article>
  );
}