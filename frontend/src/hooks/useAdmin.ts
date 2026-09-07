import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";

import { api, system } from "@/lib/api";
import { keyGroups, queryKeys } from "@/lib/queryKeys";
import type {
  ABTest,
  AuditEntry,
  HealthResponse,
  Page,
  Role,
  SeedResult,
  SystemInfo,
  User,
} from "@/lib/types";

export interface AuditFilters {
  page: number;
  pageSize: number;
  action?: string;
  resourceType?: string;
  actorId?: string;
}

export function useSystemInfo() {
  return useQuery({
    queryKey: queryKeys.systemInfo(),
    queryFn: ({ signal }) => api.get<SystemInfo>("/admin/system", undefined, signal),
  });
}

// Public readiness probe. Every role can call it, which is what the top-bar
// indicator needs; /admin/health carries the same data but is admin-only.
export function useReadiness(options: { refetchMs?: number | false } = {}) {
  return useQuery({
    queryKey: queryKeys.readiness(),
    queryFn: () => system.ready<HealthResponse>(),
    refetchInterval: options.refetchMs ?? 30_000,
    retry: false,
  });
}

export function useDependencyHealth(options: { refetchMs?: number | false } = {}) {
  return useQuery({
    queryKey: queryKeys.health(),
    queryFn: ({ signal }) => api.get<HealthResponse>("/admin/health", undefined, signal),
    refetchInterval: options.refetchMs ?? 30_000,
  });
}

export function useAuditTrail(filters: AuditFilters) {
  const query: Record<string, string | number> = {
    page: filters.page,
    page_size: filters.pageSize,
  };
  if (filters.action) query.action = filters.action;
  if (filters.resourceType) query.resource_type = filters.resourceType;
  if (filters.actorId) query.actor_id = filters.actorId;

  return useQuery({
    queryKey: queryKeys.audit(query),
    queryFn: ({ signal }) => api.get<Page<AuditEntry>>("/admin/audit", query, signal),
    placeholderData: keepPreviousData,
  });
}

export function useABTests(filters: { page: number; pageSize: number; campaignId?: string }) {
  const query: Record<string, string | number> = {
    page: filters.page,
    page_size: filters.pageSize,
  };
  if (filters.campaignId) query.campaign_id = filters.campaignId;

  return useQuery({
    queryKey: queryKeys.abTests(query),
    queryFn: ({ signal }) => api.get<Page<ABTest>>("/admin/ab-tests", query, signal),
    placeholderData: keepPreviousData,
  });
}

export function useSeed() {
  const client = useQueryClient();
  return useMutation({
    // Annotated and required: an untyped default parameter leaves TanStack Query
    // unable to infer the variables type, which collapses it to `void` and makes
    // every `mutate(...)` call a type error.
    mutationFn: (force: boolean) => api.post<SeedResult>("/admin/seed", { force }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keyGroups.campaigns });
      void client.invalidateQueries({ queryKey: keyGroups.analytics });
      void client.invalidateQueries({ queryKey: keyGroups.creatives });
    },
  });
}

export interface PruneResult {
  audit_logs_removed: number;
  idempotency_keys_removed: number;
}

// audit_days is a query parameter server-side, bounded to [30, 3650].
export function usePrune() {
  return useMutation({
    mutationFn: (auditDays: number) =>
      api.post<PruneResult>("/admin/prune", undefined, { audit_days: auditDays }),
  });
}

export function useUsers() {
  return useQuery({
    queryKey: queryKeys.users(),
    queryFn: ({ signal }) => api.get<User[]>("/auth/users", undefined, signal),
  });
}

export function useCreateUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: { email: string; password: string; role: Role; full_name?: string }) =>
      api.post<User>("/auth/users", payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.users() });
    },
  });
}

export interface UpdateUserPayload {
  role?: Role;
  is_active?: boolean;
}

/**
 * Change a role or disable an account.
 *
 * The backend revokes every session belonging to the affected account, so the
 * change takes effect on their next request rather than at token expiry. The
 * last active administrator cannot be disabled or demoted; the API answers 409
 * and the caller surfaces that message verbatim.
 */
export function useUpdateUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, payload }: { userId: string; payload: UpdateUserPayload }) =>
      api.patch<User>(`/admin/users/${encodeURIComponent(userId)}`, payload),
    onSuccess: (user) => {
      void client.invalidateQueries({ queryKey: queryKeys.users() });
      void client.invalidateQueries({ queryKey: queryKeys.user(user.id) });
    },
  });
}
