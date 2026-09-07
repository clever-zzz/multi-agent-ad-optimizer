// Minimal chart maths. Written by hand so the console has no charting runtime
// dependency and so every scale is unit-testable in isolation.

export interface Point {
  x: number;
  y: number;
}

export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
  if (min === max) {
    return min === 0 ? [0, 1] : [min * 0.9, min, min * 1.1];
  }
  const span = max - min;
  const rawStep = span / Math.max(1, count);
  const magnitude = Math.pow(10, Math.floor(Math.log10(Math.abs(rawStep) || 1)));
  const normalized = rawStep / magnitude;

  let step: number;
  if (normalized <= 1) step = 1;
  else if (normalized <= 2) step = 2;
  else if (normalized <= 2.5) step = 2.5;
  else if (normalized <= 5) step = 5;
  else step = 10;
  step *= magnitude;

  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  // Epsilon guards against float drift producing a duplicate final tick.
  for (let value = start; value <= end + step * 0.5e-6; value += step) {
    ticks.push(Math.abs(value) < step * 1e-9 ? 0 : value);
  }
  return ticks;
}

export function extent(values: number[]): [number, number] {
  const finite = values.filter((value) => Number.isFinite(value));
  if (finite.length === 0) return [0, 1];
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  return min === max ? [min - Math.abs(min || 1) * 0.1, max + Math.abs(max || 1) * 0.1] : [min, max];
}

export function makeLinearScale(domainMin: number, domainMax: number, rangeMin: number, rangeMax: number) {
  const span = domainMax - domainMin || 1;
  return (value: number): number => rangeMin + ((value - domainMin) / span) * (rangeMax - rangeMin);
}

export function linePath(points: Point[]): string {
  if (points.length === 0) return "";
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"}${round(point.x)},${round(point.y)}`)
    .join(" ");
}

// Monotone-ish smoothing using midpoint quadratics: no overshoot past the data,
// which matters because a chart that invents a spike would mislead an operator.
export function smoothPath(points: Point[]): string {
  if (points.length < 2) return linePath(points);
  const parts: string[] = [`M${round(points[0].x)},${round(points[0].y)}`];
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    const midX = (previous.x + current.x) / 2;
    parts.push(`Q${round(previous.x)},${round(previous.y)} ${round(midX)},${round((previous.y + current.y) / 2)}`);
  }
  const last = points[points.length - 1];
  parts.push(`L${round(last.x)},${round(last.y)}`);
  return parts.join(" ");
}

export function areaPath(points: Point[], baseline: number): string {
  if (points.length === 0) return "";
  const first = points[0];
  const last = points[points.length - 1];
  return `${smoothPath(points)} L${round(last.x)},${round(baseline)} L${round(first.x)},${round(baseline)} Z`;
}

export function round(value: number): number {
  return Math.round(value * 100) / 100;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function formatAxisNumber(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(abs >= 10_000 ? 0 : 1)}k`;
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 1) return value.toFixed(abs >= 10 ? 0 : 1);
  return value.toFixed(2);
}