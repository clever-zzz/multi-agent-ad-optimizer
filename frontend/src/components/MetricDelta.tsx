import { cn } from "@/lib/cn";
import { Icon } from "@/components/ui/Icon";
import { formatSignedPercent } from "@/lib/format";

export interface MetricDeltaProps {
  value: number | null | undefined;
  // Set when a rise is bad for this metric, e.g. CPA or cost.
  invertTone?: boolean;
  suffix?: string;
  className?: string;
  threshold?: number;
}

export function MetricDelta({
  value,
  invertTone = false,
  suffix,
  className,
  threshold = 0.0005,
}: MetricDeltaProps) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return <span className={cn("text-xs text-ink-3", className)}>—</span>;
  }

  const flat = Math.abs(value) < threshold;
  const rising = value > 0;
  const good = flat ? null : invertTone ? !rising : rising;

  return (
    <span
      className={cn(
        "tnum inline-flex items-center gap-1 text-xs font-medium",
        good === null && "text-ink-3",
        good === true && "text-pos",
        good === false && "text-neg",
        className,
      )}
    >
      {!flat && <Icon name={rising ? "arrowUp" : "arrowDown"} size={11} />}
      {formatSignedPercent(value, 1)}
      {suffix && <span className="font-normal text-ink-3">{suffix}</span>}
    </span>
  );
}