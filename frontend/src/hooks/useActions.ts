import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { keyGroups, queryKeys } from "@/lib/queryKeys";
import type {
  ActionStatus,
  ActionType,
  BulkActionResult,
  OptimizationAction,
  Page,
} from "@/lib/types";

export interface ActionFilters {
  page: number;
  pageSize: number;
  status?: ActionStatus | "";
  type?: ActionType | "";
  campaignId?: string;
  runId?: string;
  minConfidence?: number;
}

function toQuery(filters: ActionFilters): Record<string, string | number> {
  const query: Record<string, string | number> = {
    page: filters.page,
    page_size: filters.pageSize,
  };
  if (filters.status) query.status = filters.status;
  if (filters.type) query.type = filters.type;
  if (filters.campaignId) query.campaign_id = filters.campaignId;
  if (filters.runId) query.run_id = filters.runId;
  if (filters.minConfidence && filters.minConfidence > 0) {
    query.min_confidence = filters.minConfidence;
  }
  return query;
}

export function useActions(filters: ActionFilters) {
  const query = toQuery(filters);
  return useQuery({
    queryKey: queryKeys.actions(query),
    queryFn: ({ signal }) => api.get<Page<OptimizationAction>>("/actions", query, signal),
    placeholderData: keepPreviousData,
  });
}

function useActionInvalidator() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: keyGroups.actions });
    void client.invalidateQueries({ queryKey: keyGroups.runs });
    void client.invalidateQueries({ queryKey: keyGroups.analytics });
    void client.invalidateQueries({ queryKey: keyGroups.campaigns });
  };
}

export function useApproveAction() {
  const invalidate = useActionInvalidator();
  return useMutation({
    mutationFn: (args: { actionId: string; reason?: string; execute?: boolean }) =>
      api.post<OptimizationAction>(`/actions/${args.actionId}/approve`, {
        reason: args.reason ?? "",
        ...(args.execute === undefined ? {} : { execute: args.execute }),
      }),
    onSuccess: invalidate,
  });
}

export function useRejectAction() {
  const invalidate = useActionInvalidator();
  return useMutation({
    mutationFn: (args: { actionId: string; reason?: string }) =>
      api.post<OptimizationAction>(`/actions/${args.actionId}/reject`, {
        reason: args.reason ?? "",
      }),
    onSuccess: invalidate,
  });
}

export function useExecuteAction() {
  const invalidate = useActionInvalidator();
  return useMutation({
    mutationFn: (actionId: string) => api.post<OptimizationAction>(`/actions/${actionId}/execute`),
    onSuccess: invalidate,
  });
}

export function useBulkActions() {
  const invalidate = useActionInvalidator();
  return useMutation({
    mutationFn: (args: { actionIds: string[]; minConfidence?: number; execute?: boolean }) =>
      api.post<BulkActionResult>("/actions/bulk", {
        action_ids: args.actionIds,
        min_confidence: args.minConfidence ?? 0,
        execute: args.execute ?? false,
      }),
    onSuccess: invalidate,
  });
}