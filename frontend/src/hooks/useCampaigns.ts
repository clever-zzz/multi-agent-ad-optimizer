import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { keyGroups, queryKeys } from "@/lib/queryKeys";
import type {
  Campaign,
  CampaignCreate,
  CampaignUpdate,
  Creative,
  CreativeCreate,
  CreativeStatus,
  Page,
  Platform,
  CampaignStatus,
} from "@/lib/types";

export interface CampaignFilters {
  page: number;
  pageSize: number;
  platform?: Platform | "";
  status?: CampaignStatus | "";
  search?: string;
}

function toQuery(filters: CampaignFilters): Record<string, string | number> {
  const query: Record<string, string | number> = {
    page: filters.page,
    page_size: filters.pageSize,
  };
  if (filters.platform) query.platform = filters.platform;
  if (filters.status) query.status = filters.status;
  if (filters.search) query.search = filters.search;
  return query;
}

export function useCampaigns(filters: CampaignFilters) {
  const query = toQuery(filters);
  return useQuery({
    queryKey: queryKeys.campaigns(query),
    queryFn: ({ signal }) => api.get<Page<Campaign>>("/campaigns", query, signal),
    placeholderData: keepPreviousData,
  });
}

export function useCampaign(campaignId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.campaign(campaignId ?? ""),
    queryFn: ({ signal }) => api.get<Campaign>(`/campaigns/${campaignId}`, undefined, signal),
    enabled: Boolean(campaignId),
  });
}

export function useCreatives(campaignId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.creatives(campaignId ?? ""),
    queryFn: ({ signal }) =>
      api.get<Creative[]>(`/campaigns/${campaignId}/creatives`, undefined, signal),
    enabled: Boolean(campaignId),
  });
}

// One cached request maps ids to display names so tables never show a bare ULID.
export function useCampaignNames(): Map<string, string> {
  const query = useQuery({
    queryKey: queryKeys.campaignNames(),
    queryFn: ({ signal }) =>
      api.get<Page<Campaign>>("/campaigns", { page: 1, page_size: 200 }, signal),
    staleTime: 5 * 60_000,
  });
  return useMemo(() => {
    const map = new Map<string, string>();
    for (const campaign of query.data?.items ?? []) map.set(campaign.id, campaign.name);
    return map;
  }, [query.data]);
}

export function useCreateCampaign() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: CampaignCreate) => api.post<Campaign>("/campaigns", payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keyGroups.campaigns });
      void client.invalidateQueries({ queryKey: keyGroups.analytics });
    },
  });
}

export function useUpdateCampaign(campaignId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: CampaignUpdate) =>
      api.patch<Campaign>(`/campaigns/${campaignId}`, payload),
    onSuccess: (campaign) => {
      client.setQueryData(queryKeys.campaign(campaignId), campaign);
      void client.invalidateQueries({ queryKey: keyGroups.campaigns });
      void client.invalidateQueries({ queryKey: keyGroups.analytics });
    },
  });
}

export function useDeleteCampaign() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (campaignId: string) => api.del<void>(`/campaigns/${campaignId}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keyGroups.campaigns });
      void client.invalidateQueries({ queryKey: keyGroups.analytics });
    },
  });
}

export function useCreateCreative(campaignId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreativeCreate) =>
      api.post<Creative>(`/campaigns/${campaignId}/creatives`, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.creatives(campaignId) });
      void client.invalidateQueries({ queryKey: keyGroups.analytics });
    },
  });
}

export function useUpdateCreativeStatus(campaignId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { creativeId: string; status: CreativeStatus }) =>
      api.patch<Creative>(`/campaigns/${campaignId}/creatives/${args.creativeId}`, {
        status: args.status,
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.creatives(campaignId) });
    },
  });
}