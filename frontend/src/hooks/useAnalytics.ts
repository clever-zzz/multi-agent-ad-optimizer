import { useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type {
  Alert,
  CampaignBreakdown,
  OverviewResponse,
  PerformanceSnapshot,
  SpendSummary,
  TimeseriesPoint,
} from "@/lib/types";

export function useOverview(days: number, options: { refetchMs?: number | false } = {}) {
  return useQuery({
    queryKey: queryKeys.overview(days),
    queryFn: ({ signal }) =>
      api.get<OverviewResponse>("/analytics/overview", { days }, signal),
    refetchInterval: options.refetchMs ?? 30_000,
  });
}

// One request for every campaign's delivery, so list views can show performance
// next to configuration without an N+1 fetch pattern.
export function useSnapshots(days: number) {
  return useQuery({
    queryKey: queryKeys.snapshots(days),
    queryFn: ({ signal }) =>
      api.get<PerformanceSnapshot[]>("/analytics/snapshots", { days }, signal),
    staleTime: 30_000,
  });
}

export function useTimeseries(days: number, campaignId?: string | null) {
  const query: Record<string, string | number> = { days };
  if (campaignId) query.campaign_id = campaignId;
  return useQuery({
    queryKey: queryKeys.timeseries(days, campaignId),
    queryFn: ({ signal }) =>
      api.get<TimeseriesPoint[]>("/analytics/timeseries", query, signal),
  });
}

export function useCampaignBreakdown(campaignId: string | undefined, days: number) {
  return useQuery({
    queryKey: queryKeys.campaignBreakdown(campaignId ?? "", days),
    queryFn: ({ signal }) =>
      api.get<CampaignBreakdown>(`/analytics/campaigns/${campaignId}`, { days }, signal),
    enabled: Boolean(campaignId),
  });
}

export function useSpend(days: number) {
  return useQuery({
    queryKey: queryKeys.spend(days),
    queryFn: ({ signal }) => api.get<SpendSummary>("/analytics/llm-spend", { days }, signal),
  });
}

export function useDetectAnomalies() {
  return useMutation({
    mutationFn: (args: { days?: number; campaignId?: string }) => {
      const query: Record<string, string | number> = { days: args.days ?? 7 };
      if (args.campaignId) query.campaign_id = args.campaignId;
      return api.get<Alert[]>("/analytics/detect", query);
    },
  });
}