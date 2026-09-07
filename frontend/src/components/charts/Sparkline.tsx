import { useMemo } from "react";
import { cn } from "@/lib/cn";
import { extent, makeLinearScale, smoothPath } from "@/components/charts/chartUtils";

export interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
  fill?: boolean;
  className?: string;
}

export function Sparkline({
  values,
  width = 96,
  height = 28,
  color = "var(--color-brand-400)",
  fill = true,
  className,
}: SparklineProps) {
  const path = useMemo(() => {
    const finite = values.filter((value) => Number.isFinite(value));
    if (finite.length < 2) return { line: "", area: "" };

    const [min, max] = extent(finite);
    const stepX = width / (finite.length - 1);
    const y = makeLinearScale(min, max, height - 2, 2);
    const points = finite.map((value, index) => ({ x: index * stepX, y: y(value) }));

    const line = smoothPath(points);
    const area = `${line} L${width},${height} L0,${height} Z`;
    return { line, area };
  }, [values, width, height]);

  if (!path.line) {
    return <div className={cn("rounded bg-surface-2", className)} style={{ width, height }} />;
  }

  return (
    <svg width={width} height={height} className={cn("block overflow-visible", className)} aria-hidden="true">
      {fill && <path d={path.area} fill={color} opacity={0.14} />}
      <path d={path.line} fill="none" stroke={color} strokeWidth={1.6} strokeLinecap="round" />
    </svg>
  );
}