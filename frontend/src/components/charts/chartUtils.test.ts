import { describe, expect, it } from "vitest";

import {
  areaPath,
  clamp,
  extent,
  formatAxisNumber,
  linePath,
  makeLinearScale,
  niceTicks,
  smoothPath,
} from "@/components/charts/chartUtils";

describe("niceTicks", () => {
  it("produces round, ascending steps that cover the domain", () => {
    const ticks = niceTicks(0, 100, 4);
    expect(ticks[0]).toBe(0);
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(100);
    const steps = ticks.slice(1).map((value, index) => value - (ticks[index] ?? 0));
    expect(new Set(steps.map((step) => Math.round(step * 1e6))).size).toBe(1);
  });

  it("handles a degenerate domain without dividing by zero", () => {
    expect(niceTicks(5, 5)).toHaveLength(3);
    expect(niceTicks(0, 0)).toEqual([0, 1]);
  });

  it("does not emit duplicate ticks from float drift", () => {
    const ticks = niceTicks(0, 0.3, 3);
    expect(new Set(ticks.map((value) => value.toFixed(10))).size).toBe(ticks.length);
  });
});

describe("extent", () => {
  it("ignores non-finite samples", () => {
    expect(extent([1, Number.NaN, 5, Infinity])).toEqual([1, 5]);
  });

  it("pads a flat series so the chart is not a division by zero", () => {
    const [min, max] = extent([4, 4]);
    expect(min).toBeLessThan(4);
    expect(max).toBeGreaterThan(4);
  });

  it("falls back to a unit domain when there is no data", () => {
    expect(extent([])).toEqual([0, 1]);
  });
});

describe("makeLinearScale", () => {
  it("maps domain ends onto range ends", () => {
    const scale = makeLinearScale(0, 100, 200, 0);
    expect(scale(0)).toBe(200);
    expect(scale(100)).toBe(0);
    expect(scale(50)).toBe(100);
  });

  it("survives an inverted or empty domain", () => {
    expect(makeLinearScale(5, 5, 0, 10)(5)).toBe(0);
  });
});

describe("path builders", () => {
  const points = [
    { x: 0, y: 10 },
    { x: 10, y: 4 },
    { x: 20, y: 8 },
  ];

  it("emits an empty string for no data", () => {
    expect(linePath([])).toBe("");
    expect(areaPath([], 0)).toBe("");
  });

  it("starts a line path with a move command", () => {
    expect(linePath(points)).toMatch(/^M0,10 L10,4 L20,8$/);
  });

  it("closes an area path along the baseline", () => {
    const path = areaPath(points, 20);
    expect(path.endsWith("L20,20 L0,20 Z")).toBe(true);
  });

  it("smooths without leaving the data range, so no invented spikes appear", () => {
    const path = smoothPath(points);
    const ys = [...path.matchAll(/([\d.]+),([\d.]+)/g)].map((match) => Number(match[2]));
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(4);
    expect(Math.max(...ys)).toBeLessThanOrEqual(10);
  });

  it("degrades to a straight line with fewer than two points", () => {
    expect(smoothPath([points[0]])).toBe(linePath([points[0]]));
  });
});

describe("formatAxisNumber", () => {
  it("abbreviates magnitudes so tick labels never overlap", () => {
    expect(formatAxisNumber(0)).toBe("0.00");
    expect(formatAxisNumber(1500)).toBe("1.5k");
    expect(formatAxisNumber(25000)).toBe("25k");
    expect(formatAxisNumber(2_500_000)).toBe("2.5M");
    expect(formatAxisNumber(12.5)).toBe("13");
  });
});

describe("clamp", () => {
  it("bounds a value on both sides", () => {
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(-5, 0, 10)).toBe(0);
    expect(clamp(50, 0, 10)).toBe(10);
  });
});