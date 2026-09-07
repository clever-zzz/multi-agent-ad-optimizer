import { useState } from "react";
import { cn } from "@/lib/cn";

export interface DonutSlice {
  id: string;
  label: string;
  value: number;
  color: string;
}

export interface DonutProps {
  slices: DonutSlice[];
  size?: number;
  thickness?: number;
  centerLabel?: string;
  centerValue?: string;
  format?: (value: number) => string;
  className?: string;
}

function polar(cx: number, cy: number, radius: number, angleDegrees: number): [number, number] {
  const radians = ((angleDegrees - 90) * Math.PI) / 180;
  return [cx + radius * Math.cos(radians), cy + radius * Math.sin(radians)];
}

function arcPath(cx: number, cy: number, radius: number, start: number, end: number): string {
  const sweep = Math.min(359.999, Math.max(0.001, end - start));
  const [sx, sy] = polar(cx, cy, radius, start);
  const [ex, ey] = polar(cx, cy, radius, start + sweep);
  const largeArc = sweep > 180 ? 1 : 0;
  return `M${sx.toFixed(2)},${sy.toFixed(2)} A${radius},${radius} 0 ${largeArc} 1 ${ex.toFixed(2)},${ey.toFixed(2)}`;
}

export function Donut({
  slices,
  size = 168,
  thickness = 18,
  centerLabel,
  centerValue,
  format = (value) => String(Math.round(value)),
  className,
}: DonutProps) {
  const [active, setActive] = useState<string | null>(null);
  const total = slices.reduce((sum, slice) => sum + Math.max(0, slice.value), 0);
  const radius = (size - thickness) / 2;
  const center = size / 2;

  if (total <= 0) {
    return (
      <div className={cn("grid place-items-center", className)} style={{ width: size, height: size }}>
        <p className="text-xs text-ink-3">No data</p>
      </div>
    );
  }

  let cursor = 0;
  const arcs = slices.map((slice) => {
    const sweep = (Math.max(0, slice.value) / total) * 360;
    const arc = { slice, start: cursor, end: cursor + sweep };
    cursor += sweep;
    return arc;
  });

  const activeSlice = arcs.find((arc) => arc.slice.id === active)?.slice;

  return (
    <div className={cn("flex flex-wrap items-center gap-5", className)}>
      <div className="relative shrink-0" style={{ width: size, height: size }}>
        <svg width={size} height={size} role="img" aria-label={centerLabel ?? "Distribution"}>
          <circle
            cx={center}
            cy={center}
            r={radius}
            fill="none"
            stroke="var(--color-surface-3)"
            strokeWidth={thickness}
          />
          {arcs.map(({ slice, start, end }) => (
            <path
              key={slice.id}
              d={arcPath(center, center, radius, start, end)}
              fill="none"
              stroke={slice.color}
              strokeWidth={active === slice.id ? thickness + 3 : thickness}
              strokeLinecap="butt"
              opacity={active === null || active === slice.id ? 1 : 0.35}
              onMouseEnter={() => setActive(slice.id)}
              onMouseLeave={() => setActive(null)}
              className="transition-all duration-150"
            />
          ))}
        </svg>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
          <span className="tnum text-lg font-semibold text-ink-1">
            {activeSlice ? format(activeSlice.value) : (centerValue ?? format(total))}
          </span>
          <span className="max-w-24 truncate text-[11px] text-ink-3">
            {activeSlice ? activeSlice.label : (centerLabel ?? "Total")}
          </span>
        </div>
      </div>

      <ul className="flex min-w-32 flex-1 flex-col gap-1.5">
        {arcs.map(({ slice }) => {
          const share = (Math.max(0, slice.value) / total) * 100;
          return (
            <li
              key={slice.id}
              onMouseEnter={() => setActive(slice.id)}
              onMouseLeave={() => setActive(null)}
              className={cn(
                "flex items-center gap-2 rounded-md px-1.5 py-1 transition-colors",
                active === slice.id && "bg-surface-2",
              )}
            >
              <span className="size-2 shrink-0 rounded-sm" style={{ background: slice.color }} />
              <span className="min-w-0 flex-1 truncate text-xs text-ink-2">{slice.label}</span>
              <span className="tnum shrink-0 text-xs font-medium text-ink-1">
                {share.toFixed(1)}%
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}