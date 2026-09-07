import { useQuery, keepPreviousData } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type { Creative, CreativeStatus, Page } from "@/lib/types";

export interface CreativeFilters {
  page: number;
  pageSize: number;
  campaignId?: string;
  status?: CreativeStatus | "";
  origin?: "human" | "llm" | "rule" | "";
  abGroup?: string;
  type?: "text" | "image" | "video" | "";
  search?: string;
  sort?: "created_at" | "score" | "headline" | "status";
  order?: "asc" | "desc";
}

export interface CreativeSummary {
  total: number;
  by_origin: Record<string, number>;
  by_status: Record<string, number>;
  generated: number;
  generated_share: number;
  scored: number;
}

function toQuery(filters: CreativeFilters): Record<string, string | number> {
  const query: Record<string, string | number> = {
    page: filters.page,
    page_size: filters.pageSize,
    sort: filters.sort ?? "created_at",
    order: filters.order ?? "desc",
  };
  if (filters.campaignId) query.campaign_id = filters.campaignId;
  if (filters.status) query.status = filters.status;
  if (filters.origin) query.origin = filters.origin;
  if (filters.abGroup) query.ab_group = filters.abGroup;
  if (filters.type) query.type = filters.type;
  if (filters.search) query.search = filters.search;
  return query;
}

export function useCreativeLibrary(filters: CreativeFilters) {
  const query = toQuery(filters);
  return useQuery({
    queryKey: queryKeys.allCreatives(query),
    queryFn: ({ signal }) => api.get<Page<Creative>>("/creatives", query, signal),
    placeholderData: keepPreviousData,
  });
}

export function useCreativeSummary() {
  return useQuery({
    queryKey: queryKeys.creativeSummary(),
    queryFn: ({ signal }) => api.get<CreativeSummary>("/creatives/summary", undefined, signal),
  });
}