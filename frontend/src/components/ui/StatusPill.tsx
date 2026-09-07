import { Badge, type BadgeTone } from "@/components/ui/Badge";
import { humanize } from "@/lib/format";

// One mapping table per domain enum keeps colour semantics identical everywhere.

const CAMPAIGN: Record<string, BadgeTone> = {
  active: "positive",
  paused: "warning",
  completed: "neutral",
  archived: "neutral",
};

const RUN: Record<string, BadgeTone> = {
  pending: "neutral",
  running: "info",
  succeeded: "positive",
  failed: "negative",
  cancelled: "warning",
};

const ACTION: Record<string, BadgeTone> = {
  proposed: "brand",
  approved: "info",
  rejected: "neutral",
  executed: "positive",
  failed: "negative",
  skipped: "warning",
};

const ALERT: Record<string, BadgeTone> = {
  open: "negative",
  acknowledged: "warning",
  resolved: "positive",
};

const SEVERITY: Record<string, BadgeTone> = {
  info: "info",
  warning: "warning",
  critical: "negative",
};

const CREATIVE: Record<string, BadgeTone> = {
  draft: "neutral",
  active: "positive",
  paused: "warning",
  rejected: "negative",
};

const PLATFORM: Record<string, BadgeTone> = {
  google: "brand",
  meta: "violet",
  tiktok: "info",
  mock: "neutral",
};

const ABTEST: Record<string, BadgeTone> = {
  draft: "neutral",
  running: "info",
  concluded: "positive",
  cancelled: "warning",
};

export type StatusDomain =
  | "campaign"
  | "run"
  | "action"
  | "alert"
  | "severity"
  | "creative"
  | "platform"
  | "abtest";

const MAPS: Record<StatusDomain, Record<string, BadgeTone>> = {
  campaign: CAMPAIGN,
  run: RUN,
  action: ACTION,
  alert: ALERT,
  severity: SEVERITY,
  creative: CREATIVE,
  platform: PLATFORM,
  abtest: ABTEST,
};

export interface StatusPillProps {
  domain: StatusDomain;
  value: string;
  dot?: boolean;
  label?: string;
  className?: string;
}

export function StatusPill({ domain, value, dot = true, label, className }: StatusPillProps) {
  const tone = MAPS[domain][value] ?? "neutral";
  return (
    <Badge tone={tone} dot={dot} className={className}>
      {label ?? humanize(value)}
    </Badge>
  );
}