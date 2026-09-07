import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";

function isTransient(error: unknown): boolean {
  if (error instanceof ApiError) {
    // 4xx (except 408/429) are deterministic; retrying only burns time.
    if (error.status >= 400 && error.status < 500) {
      return error.status === 408 || error.status === 429;
    }
    return true;
  }
  return true;
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => isTransient(error) && failureCount < 2,
    },
    mutations: {
      retry: false,
    },
  },
});