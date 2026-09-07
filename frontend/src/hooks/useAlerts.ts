import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { keyGroups, queryKeys } from "@/lib/queryKeys";
import type {
  Alert,
  AlertRule,
  AlertSeverity,
  AlertStatus,
  Page,
} from "@/lib/types";

export interface AlertFilters {
  page: number;
  pageSize: number;
  status?: AlertStatus | "";
  severity?: AlertSeverity | "";
  rule?: AlertRule | "";
  campaignId?: string;
}

export interface AlertTrendPoint {
  rule: string;
  severity: string;
  total: number;
}

/** Mirrors `AlertSummaryOut` on the server. */
export interface AlertSummary {
  /** Not resolved yet: open plus acknowledged. */
  open: number;
  acknowledged: number;
  resolved: number;
  by_severity: Record<string, number>;
  history: AlertTrendPoint[];
}

export function useAlerts(filters: AlertFilters) {
  const query: Record<string, string | number> = {
    page: filters.page,
    page_size: filters.pageSize,
  };
  if (filters.status) query.status = filters.status;
  if (filters.severity) query.severity = filters.severity;
  if (filters.rule) query.rule = filters.rule;
  if (filters.campaignId) query.campaign_id = filters.campaignId;

  return useQuery({
    queryKey: queryKeys.alerts(query),
    queryFn: ({ signal }) => api.get<Page<Alert>>("/alerts", query, signal),
    placeholderData: keepPreviousData,
  });
}

export function useAlertSummary() {
  return useQuery({
    queryKey: queryKeys.alertSummary(),
    queryFn: ({ signal }) => api.get<AlertSummary>("/alerts/summary", undefined, signal),
    refetchInterval: 30_000,
  });
}

function useAlertInvalidator() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: keyGroups.alerts });
    void client.invalidateQueries({ queryKey: keyGroups.analytics });
  };
}

export function useAcknowledgeAlert() {
  const invalidate = useAlertInvalidator();
  return useMutation({
    mutationFn: (args: { alertId: string; note?: string }) =>
      api.post<Alert>(`/alerts/${args.alertId}/acknowledge`, { note: args.note ?? "" }),
    onSuccess: invalidate,
  });
}

export function useResolveAlert() {
  const invalidate = useAlertInvalidator();
  return useMutation({
    mutationFn: (args: { alertId: string; note?: string }) =>
      api.post<Alert>(`/alerts/${args.alertId}/resolve`, { note: args.note ?? "" }),
    onSuccess: invalidate,
  });
}