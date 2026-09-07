// Central cache-key registry. Every invalidation names a key from here so no
// component invents its own string and silently misses a refresh.

export const queryKeys = {
  overview: (days: number) => ["analytics", "overview", days] as const,
  snapshots: (days: number) => ["analytics", "snapshots", days] as const,
  timeseries: (days: number, campaignId?: string | null) =>
    ["analytics", "timeseries", days, campaignId ?? "all"] as const,
  campaignBreakdown: (campaignId: string, days: number) =>
    ["analytics", "campaign", campaignId, days] as const,
  spend: (days: number) => ["analytics", "spend", days] as const,
  detect: (days: number, campaignId?: string | null) =>
    ["analytics", "detect", days, campaignId ?? "all"] as const,

  campaigns: (filters: Record<string, unknown>) => ["campaigns", "list", filters] as const,
  campaign: (id: string) => ["campaigns", "detail", id] as const,
  campaignNames: () => ["campaigns", "names"] as const,
  creatives: (campaignId: string) => ["campaigns", "creatives", campaignId] as const,
  allCreatives: (filters: Record<string, unknown>) => ["creatives", "list", filters] as const,
  creativeSummary: () => ["creatives", "summary"] as const,
  campaignMetrics: (id: string, days: number) => ["campaigns", "metrics", id, days] as const,

  runs: (filters: Record<string, unknown>) => ["runs", "list", filters] as const,
  run: (id: string) => ["runs", "detail", id] as const,
  runEvents: (id: string) => ["runs", "events", id] as const,

  actions: (filters: Record<string, unknown>) => ["actions", "list", filters] as const,
  action: (id: string) => ["actions", "detail", id] as const,

  alerts: (filters: Record<string, unknown>) => ["alerts", "list", filters] as const,
  alertSummary: () => ["alerts", "summary"] as const,

  abTests: (filters: Record<string, unknown>) => ["admin", "ab-tests", filters] as const,
  audit: (filters: Record<string, unknown>) => ["admin", "audit", filters] as const,
  systemInfo: () => ["admin", "system"] as const,
  health: () => ["admin", "health"] as const,
  readiness: () => ["system", "readiness"] as const,
  users: () => ["admin", "users"] as const,
  user: (id: string) => ["admin", "users", id] as const,
} as const;

export const keyGroups = {
  campaigns: ["campaigns"] as const,
  creatives: ["creatives"] as const,
  runs: ["runs"] as const,
  actions: ["actions"] as const,
  alerts: ["alerts"] as const,
  analytics: ["analytics"] as const,
  admin: ["admin"] as const,
} as const;