import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { streamRun } from "@/lib/sse";
import { keyGroups, queryKeys } from "@/lib/queryKeys";
import type { Page, Run, RunDetail, RunStartRequest, RunStatus, StreamFrame } from "@/lib/types";

export interface RunFilters {
  page: number;
  pageSize: number;
  status?: RunStatus | "";
}

export function useRuns(filters: RunFilters) {
  const query: Record<string, string | number> = {
    page: filters.page,
    page_size: filters.pageSize,
  };
  if (filters.status) query.status = filters.status;

  return useQuery({
    queryKey: queryKeys.runs(query),
    queryFn: ({ signal }) => api.get<Page<Run>>("/runs", query, signal),
    placeholderData: keepPreviousData,
  });
}

export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  "succeeded",
  "failed",
  "cancelled",
]);

// Polling stops by itself once the run reaches a terminal state, so an open
// detail tab never keeps hitting the API for a run that finished hours ago.
export function useRun(runId: string | undefined, options: { refetchMs?: number } = {}) {
  const interval = options.refetchMs ?? 6000;
  return useQuery({
    queryKey: queryKeys.run(runId ?? ""),
    queryFn: ({ signal }) => api.get<RunDetail>(`/runs/${runId}`, undefined, signal),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const run = query.state.data?.run;
      if (!run) return interval;
      return TERMINAL_RUN_STATUSES.has(run.status) ? false : interval;
    },
  });
}

export function useStartRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: RunStartRequest) => api.post<Run>("/runs", payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keyGroups.runs });
      void client.invalidateQueries({ queryKey: keyGroups.analytics });
    },
  });
}

export function useCancelRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.post<Run>(`/runs/${runId}/cancel`),
    onSuccess: (run) => {
      client.setQueryData(queryKeys.run(run.id), (prev: RunDetail | undefined) =>
        prev ? { ...prev, run } : prev,
      );
      void client.invalidateQueries({ queryKey: keyGroups.runs });
    },
  });
}

// Live run view: subscribes to SSE, keeps an ordered frame buffer and exposes
// connection state so the page can show a live/reconnecting indicator.
export interface RunStreamState {
  frames: StreamFrame[];
  connected: boolean;
  error: string | null;
  finished: boolean;
  lastSeq: number;
  clear: () => void;
}

export function useRunStream(runId: string | undefined, enabled = true): RunStreamState {
  const client = useQueryClient();
  const [frames, setFrames] = useState<StreamFrame[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const lastSeqRef = useRef(0);
  const seenRef = useRef<Set<string>>(new Set());

  const clear = useCallback(() => {
    setFrames([]);
    seenRef.current = new Set();
    lastSeqRef.current = 0;
    setFinished(false);
    setError(null);
  }, []);

  useEffect(() => {
    if (!runId || !enabled || finished) return;

    const controller = new AbortController();
    setConnected(true);
    setError(null);

    void streamRun(
      runId,
      {
        onFrame: (frame) => {
          if (frame.seq < 0) return; // heartbeat
          if (seenRef.current.has(frame.id)) return;
          seenRef.current.add(frame.id);
          lastSeqRef.current = Math.max(lastSeqRef.current, frame.seq);
          setFrames((prev) => [...prev, frame].slice(-500));

          if (TERMINAL_RUN_STATUSES.has(frame.type.replace("run.", ""))) {
            setFinished(true);
            void client.invalidateQueries({ queryKey: queryKeys.run(runId) });
            void client.invalidateQueries({ queryKey: keyGroups.actions });
            void client.invalidateQueries({ queryKey: keyGroups.analytics });
            void client.invalidateQueries({ queryKey: keyGroups.alerts });
          }
        },
        onClosed: () => {
          setConnected(false);
          setFinished(true);
          void client.invalidateQueries({ queryKey: queryKeys.run(runId) });
        },
        onError: (err) => {
          setConnected(false);
          setError(err.message);
        },
      },
      { lastSeq: lastSeqRef.current, signal: controller.signal },
    );

    return () => {
      controller.abort();
      setConnected(false);
    };
  }, [runId, enabled, finished, client]);

  return {
    frames,
    connected,
    error,
    finished,
    lastSeq: lastSeqRef.current,
    clear,
  };
}