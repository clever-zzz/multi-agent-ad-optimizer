import { describe, expect, it } from "vitest";

import {
  formatCompact,
  formatCurrency,
  formatDate,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  formatRatio,
  formatSignedPercent,
  formatRelative,
  humanize,
  pctChange,
  truncate,
} from "@/lib/format";

describe("formatNumber", () => {
  it("groups thousands and honours fraction digits", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
    expect(formatNumber(1234.5678, 2)).toBe("1,234.57");
  });

  it("returns an em dash for missing values instead of NaN", () => {
    expect(formatNumber(null)).toBe("—");
    expect(formatNumber(undefined)).toBe("—");
    expect(formatNumber(Number.NaN)).toBe("—");
  });
});

describe("formatCompact", () => {
  it("abbreviates large magnitudes", () => {
    expect(formatCompact(1500)).toBe("1.5K");
    expect(formatCompact(2_500_000)).toBe("2.5M");
  });
});

describe("formatCurrency", () => {
  it("renders USD with two decimals by default", () => {
    expect(formatCurrency(1234.5)).toBe("$1,234.50");
  });

  it("supports compact notation for headline figures", () => {
    expect(formatCurrency(1_250_000, { compact: true })).toBe("$1.3M");
  });

  it("handles zero and negative values", () => {
    expect(formatCurrency(0)).toBe("$0.00");
    expect(formatCurrency(-42.1)).toBe("-$42.10");
  });
});

describe("formatPercent", () => {
  it("converts a backend fraction into a percentage", () => {
    expect(formatPercent(0.0234)).toBe("2.34%");
    expect(formatPercent(0.0234, 1)).toBe("2.3%");
    expect(formatPercent(1)).toBe("100.00%");
  });

  it("does not render 0% for missing data", () => {
    expect(formatPercent(null)).toBe("—");
  });
});

describe("formatRatio and formatSignedPercent", () => {
  it("appends the multiplier suffix", () => {
    expect(formatRatio(2.5)).toBe("2.50x");
    expect(formatRatio(null)).toBe("—");
  });

  it("signs deltas explicitly so direction is unambiguous", () => {
    expect(formatSignedPercent(0.125)).toBe("+12.5%");
    expect(formatSignedPercent(-0.08)).toBe("-8.0%");
    expect(formatSignedPercent(0)).toBe("0.0%");
  });
});

describe("date helpers", () => {
  it("renders a stable, locale-formatted timestamp", () => {
    const value = "2026-03-04T13:05:09Z";
    expect(formatDateTime(value)).toContain("2026");
    expect(formatDateTime(value)).toContain("Mar");
    expect(formatDate(value)).toContain("Mar");
  });

  it("falls back to a dash for unparseable input", () => {
    expect(formatDateTime("not-a-date")).toBe("—");
    expect(formatDate(null)).toBe("—");
  });
});

describe("formatRelative", () => {
  const now = Date.parse("2026-05-01T12:00:00Z");

  it("describes the past in human units", () => {
    expect(formatRelative(new Date(now - 5_000).toISOString(), now)).toBe("just now");
    expect(formatRelative(new Date(now - 120_000).toISOString(), now)).toBe("2 minutes ago");
    expect(formatRelative(new Date(now - 3_600_000).toISOString(), now)).toBe("1 hour ago");
    expect(formatRelative(new Date(now - 172_800_000).toISOString(), now)).toBe("2 days ago");
  });

  it("describes the future symmetrically", () => {
    expect(formatRelative(new Date(now + 3_600_000).toISOString(), now)).toBe("1 hour from now");
  });

  it("handles missing input", () => {
    expect(formatRelative(null, now)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("scales from seconds to hours", () => {
    const start = "2026-05-01T12:00:00Z";
    expect(formatDuration(start, "2026-05-01T12:00:42Z")).toBe("42s");
    expect(formatDuration(start, "2026-05-01T12:03:05Z")).toBe("3m 5s");
    expect(formatDuration(start, "2026-05-01T14:20:00Z")).toBe("2h 20m");
  });

  it("never reports negative elapsed time from clock skew", () => {
    expect(formatDuration("2026-05-01T12:00:00Z", "2026-05-01T11:59:00Z")).toBe("0s");
  });
});

describe("humanize", () => {
  it("turns snake_case enum values into labels", () => {
    expect(humanize("pause_creative")).toBe("Pause Creative");
    expect(humanize("low_roas")).toBe("Low Roas");
    expect(humanize("")).toBe("—");
  });
});

describe("truncate", () => {
  it("shortens with an ellipsis and leaves short strings alone", () => {
    expect(truncate("abcdefghij", 5)).toBe("abcd…");
    expect(truncate("abc", 5)).toBe("abc");
  });
});

describe("pctChange", () => {
  it("computes relative change and guards against division by zero", () => {
    expect(pctChange(100, 125)).toBeCloseTo(0.25);
    expect(pctChange(0, 125)).toBe(0);
  });
});