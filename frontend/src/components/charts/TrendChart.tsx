import { useMemo, useState, type MouseEvent as ReactMouseEvent } from "react";
import { cn } from "@/lib/cn";
import { useMeasure } from "@/hooks/useMeasure";
import {
  areaPath,
  clamp,
  extent,
  formatAxisNumber,
  linePath,
  makeLinearScale,
  niceTicks,
  round,
  smoothPath,
} from "@/components/charts/chartUtils";

export interface TrendSeries {
  key: string;
  label: string;
  color: string;
  axis?: "left" | "right";
  kind?: "line" | "area" | "bar";
  format?: (value: number) => string;
  dashed?: boolean;
}

export interface TrendDatum {
  label: string;
  values: Record<string, number | null>;
}

export interface TrendChartProps {
  data: TrendDatum[];
  series: TrendSeries[];
  height?: number;
  showLegend?: boolean;
  className?: string;
  emptyLabel?: string;
  yLeftLabel?: string;
  yRightLabel?: string;
  maxXTicks?: number;
}

const PAD = { top: 12, right: 12, bottom: 26, left: 46 };

export function TrendChart({
  data,
  series,
  height = 240,
  showLegend = true,
  className,
  emptyLabel = "No data in this window",
  yLeftLabel,
  yRightLabel,
  maxXTicks = 8,
}: TrendChartProps) {
  const [ref, size] = useMeasure<HTMLDivElement>({ width: 0, height });
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  const hasRight = series.some((item) => (item.axis ?? "left") === "right");
  const width = Math.max(size.width, 240);
  const padRight = hasRight ? PAD.right + 40 : PAD.right;

  const model = useMemo(() => {
    const visible = series.filter((item) => !hidden.has(item.key));
    const innerWidth = Math.max(10, width - PAD.left - padRight);
    const innerHeight = Math.max(10, height - PAD.top - PAD.bottom);

    const step = data.length > 1 ? innerWidth / (data.length - 1) : innerWidth;
    const xAt = (index: number): number =>
      PAD.left + (data.length > 1 ? index * step : innerWidth / 2);

    const collect = (axis: "left" | "right"): number[] =>
      data.flatMap((datum) =>
        visible
          .filter((item) => (item.axis ?? "left") === axis && item.kind !== "bar")
          .map((item) => datum.values[item.key])
          .filter((value): value is number => typeof value === "number" && Number.isFinite(value)),
      );

    const leftValues = collect("left");
    const rightValues = collect("right");
    const barValues = data.flatMap((datum) =>
      visible
        .filter((item) => item.kind === "bar")
        .map((item) => datum.values[item.key])
        .filter((value): value is number => typeof value === "number" && Number.isFinite(value)),
    );

    const [leftMin, leftMax] = extent([...leftValues, ...barValues]);
    const [rightMin, rightMax] = extent(rightValues);

    const leftFloor = Math.min(0, leftMin);
    const yLeft = makeLinearScale(leftFloor, Math.max(leftMax, leftFloor + 1e-9), height - PAD.bottom, PAD.top);
    const yRight = makeLinearScale(
      Math.min(0, rightMin),
      Math.max(rightMax, Math.min(0, rightMin) + 1e-9),
      height - PAD.bottom,
      PAD.top,
    );

    const leftTicks = niceTicks(leftFloor, leftMax, 4);
    const rightTicks = hasRight ? niceTicks(Math.min(0, rightMin), rightMax, 4) : [];

    return {
      visible,
      innerWidth,
      innerHeight,
      step,
      xAt,
      yLeft,
      yRight,
      leftTicks,
      rightTicks,
      baseline: yLeft(Math.max(leftFloor, 0)),
      barWidth: Math.max(2, Math.min(26, step * 0.55)),
    };
  }, [data, series, hidden, width, height, padRight, hasRight]);

  if (data.length === 0) {
    return (
      <div className={cn("grid place-items-center rounded-lg border border-dashed border-line", className)} style={{ height }}>
        <p className="text-xs text-ink-3">{emptyLabel}</p>
      </div>
    );
  }

  const { xAt, yLeft, yRight, leftTicks, rightTicks, baseline, barWidth } = model;

  const xTickStride = Math.max(1, Math.ceil(data.length / maxXTicks));
  const hoverX = hoverIndex === null ? null : xAt(hoverIndex);

  const onMove = (event: ReactMouseEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const offset = event.clientX - box.left - PAD.left;
    const index = model.step > 0 ? Math.round(offset / model.step) : 0;
    setHoverIndex(clamp(index, 0, data.length - 1));
  };

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div ref={ref} className="relative w-full" style={{ height }}>
        <svg
          width={width}
          height={height}
          role="img"
          onMouseMove={onMove}
          onMouseLeave={() => setHoverIndex(null)}
          className="block touch-none select-none"
        >
          {leftTicks.map((tick) => (
            <g key={`ly-${tick}`}>
              <line
                x1={PAD.left}
                x2={width - padRight}
                y1={round(yLeft(tick))}
                y2={round(yLeft(tick))}
                stroke="var(--color-line)"
                strokeWidth={1}
                strokeDasharray={tick === 0 ? undefined : "3 4"}
              />
              <text
                x={PAD.left - 8}
                y={round(yLeft(tick)) + 3.5}
                textAnchor="end"
                className="fill-[var(--color-ink-3)] text-[10px] tnum"
              >
                {formatAxisNumber(tick)}
              </text>
            </g>
          ))}

          {hasRight &&
            rightTicks.map((tick) => (
              <text
                key={`ry-${tick}`}
                x={width - padRight + 8}
                y={round(yRight(tick)) + 3.5}
                textAnchor="start"
                className="fill-[var(--color-ink-3)] text-[10px] tnum"
              >
                {formatAxisNumber(tick)}
              </text>
            ))}

          {data.map((datum, index) =>
            index % xTickStride === 0 ? (
              <text
                key={`x-${datum.label}-${index}`}
                x={round(xAt(index))}
                y={height - 8}
                textAnchor="middle"
                className="fill-[var(--color-ink-3)] text-[10px]"
              >
                {datum.label}
              </text>
            ) : null,
          )}

          {model.visible
            .filter((item) => item.kind === "bar")
            .flatMap((item) =>
              data.map((datum, index) => {
                const value = datum.values[item.key];
                if (typeof value !== "number" || !Number.isFinite(value)) return null;
                const y = yLeft(value);
                const top = Math.min(y, baseline);
                return (
                  <rect
                    key={`bar-${item.key}-${index}`}
                    x={round(xAt(index) - barWidth / 2)}
                    y={round(top)}
                    width={round(barWidth)}
                    height={round(Math.max(1, Math.abs(baseline - y)))}
                    rx={2}
                    fill={item.color}
                    opacity={hoverIndex === null || hoverIndex === index ? 0.55 : 0.22}
                  />
                );
              }),
            )}

          {model.visible
            .filter((item) => item.kind !== "bar")
            .map((item) => {
              const scale = (item.axis ?? "left") === "right" ? yRight : yLeft;
              const points = data
                .map((datum, index) => ({ datum, index }))
                .filter(({ datum }) => {
                  const value = datum.values[item.key];
                  return typeof value === "number" && Number.isFinite(value);
                })
                .map(({ datum, index }) => ({ x: xAt(index), y: scale(datum.values[item.key] as number) }));

              if (points.length === 0) return null;
              const path = item.kind === "area" ? smoothPath(points) : linePath(points);

              return (
                <g key={item.key}>
                  {item.kind === "area" && (
                    <path d={areaPath(points, baseline)} fill={item.color} opacity={0.13} />
                  )}
                  <path
                    d={path}
                    fill="none"
                    stroke={item.color}
                    strokeWidth={1.9}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeDasharray={item.dashed ? "5 4" : undefined}
                  />
                </g>
              );
            })}

          {hoverIndex !== null && hoverX !== null && (
            <g>
              <line
                x1={round(hoverX)}
                x2={round(hoverX)}
                y1={PAD.top}
                y2={height - PAD.bottom}
                stroke="var(--color-line-strong)"
                strokeWidth={1}
              />
              {model.visible
                .filter((item) => item.kind !== "bar")
                .map((item) => {
                  const value = data[hoverIndex]?.values[item.key];
                  if (typeof value !== "number" || !Number.isFinite(value)) return null;
                  const scale = (item.axis ?? "left") === "right" ? yRight : yLeft;
                  return (
                    <circle
                      key={`dot-${item.key}`}
                      cx={round(hoverX)}
                      cy={round(scale(value))}
                      r={3.2}
                      fill="var(--color-surface-0)"
                      stroke={item.color}
                      strokeWidth={2}
                    />
                  );
                })}
            </g>
          )}
        </svg>

        {hoverIndex !== null && data[hoverIndex] && (
          <div
            className="card pointer-events-none absolute z-10 min-w-40 px-2.5 py-2 shadow-xl shadow-black/50"
            style={{
              left: clamp((hoverX ?? 0) + 12, 4, Math.max(4, width - 168)),
              top: PAD.top,
            }}
          >
            <p className="mb-1 text-[11px] font-semibold text-ink-1">{data[hoverIndex].label}</p>
            <ul className="flex flex-col gap-0.5">
              {series.map((item) => {
                const value = data[hoverIndex].values[item.key];
                return (
                  <li key={item.key} className="flex items-center justify-between gap-3 text-[11px]">
                    <span className="flex items-center gap-1.5 text-ink-3">
                      <span className="size-1.5 rounded-full" style={{ background: item.color }} />
                      {item.label}
                    </span>
                    <span className="tnum font-medium text-ink-1">
                      {typeof value === "number" && Number.isFinite(value)
                        ? (item.format ? item.format(value) : formatAxisNumber(value))
                        : "—"}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>

      {showLegend && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
          {series.map((item) => {
            const off = hidden.has(item.key);
            return (
              <button
                key={item.key}
                type="button"
                onClick={() =>
                  setHidden((prev) => {
                    const next = new Set(prev);
                    if (next.has(item.key)) next.delete(item.key);
                    else next.add(item.key);
                    return next;
                  })
                }
                className={cn(
                  "inline-flex items-center gap-1.5 text-[11px] transition-opacity",
                  off ? "text-ink-3 opacity-45 line-through" : "text-ink-2",
                )}
                aria-pressed={!off}
              >
                <span
                  className="h-0.5 w-3.5 rounded-full"
                  style={{ background: off ? "var(--color-ink-3)" : item.color }}
                />
                {item.label}
                {(item.axis ?? "left") === "right" && yRightLabel && (
                  <span className="text-ink-3">({yRightLabel})</span>
                )}
              </button>
            );
          })}
          {yLeftLabel && <span className="ml-auto text-[11px] text-ink-3">left axis: {yLeftLabel}</span>}
        </div>
      )}
    </div>
  );
}