// Presentation helpers. Every number that reaches the screen goes through here so
// formatting stays consistent across pages and stays trivially unit-testable.

const DASH = "—";

export function isNil(value: unknown): boolean {
  return value === null || value === undefined || Number.isNaN(value);
}

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (isNil(value)) return DASH;
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatCompact(value: number | null | undefined): string {
  if (isNil(value)) return DASH;
  return Number(value).toLocaleString("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  });
}

export function formatCurrency(
  value: number | null | undefined,
  options: { compact?: boolean; digits?: number } = {},
): string {
  if (isNil(value)) return DASH;
  const { compact = false, digits = 2 } = options;
  return Number(value).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    notation: compact ? "compact" : "standard",
    minimumFractionDigits: compact ? 0 : digits,
    maximumFractionDigits: compact ? 1 : digits,
  });
}

// Backend rates are fractions (0.0234), the UI shows percentages (2.34%).
export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (isNil(value)) return DASH;
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

export function formatRatio(value: number | null | undefined, digits = 2): string {
  if (isNil(value)) return DASH;
  return `${Number(value).toFixed(digits)}x`;
}

export function formatSignedPercent(value: number | null | undefined, digits = 1): string {
  if (isNil(value)) return DASH;
  const pct = Number(value) * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(digits)}%`;
}

export function formatDateTime(value: string | Date | null | undefined): string {
  if (!value) return DASH;
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return DASH;
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function formatDate(value: string | Date | null | undefined): string {
  if (!value) return DASH;
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return DASH;
  return date.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "2-digit" });
}

export function formatTime(value: string | Date | null | undefined): string {
  if (!value) return DASH;
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return DASH;
  return date.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function formatRelative(value: string | Date | null | undefined, now = Date.now()): string {
  if (!value) return DASH;
  const date = typeof value === "string" ? new Date(value) : value;
  const time = date.getTime();
  if (Number.isNaN(time)) return DASH;

  const diffSeconds = Math.round((time - now) / 1000);
  const abs = Math.abs(diffSeconds);
  const suffix = diffSeconds < 0 ? "ago" : "from now";

  if (abs < 45) return diffSeconds < 0 ? "just now" : "in a moment";
  const units: Array<[number, string]> = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
    [604800, "week"],
    [2592000, "month"],
  ];
  for (let i = units.length - 1; i >= 0; i -= 1) {
    const [seconds, label] = units[i];
    if (abs >= seconds) {
      const amount = Math.round(abs / seconds);
      return `${amount} ${label}${amount === 1 ? "" : "s"} ${suffix}`;
    }
  }
  return `${abs} seconds ${suffix}`;
}

export function formatDuration(start: string | null | undefined, end?: string | null): string {
  if (!start) return DASH;
  const from = new Date(start).getTime();
  const to = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(from) || Number.isNaN(to)) return DASH;
  const total = Math.max(0, Math.round((to - from) / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes}m ${seconds}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

// Converts snake_case enum values into stable display labels.
export function humanize(value: string | null | undefined): string {
  if (!value) return DASH;
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function truncate(value: string, max = 80): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
}

export function pctChange(before: number, after: number): number {
  if (!before) return 0;
  return (after - before) / before;
}