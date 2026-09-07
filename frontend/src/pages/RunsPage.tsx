import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { RunTriggerDialog } from "@/components/RunTriggerDialog";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { SelectField } from "@/components/ui/Field";
import { Table, type Column } from "@/components/ui/Table";
import { Pagination } from "@/components/ui/Pagination";
import { StatusPill } from "@/components/ui/StatusPill";
import { Badge } from "@/components/ui/Badge";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { useRuns } from "@/hooks/useRuns";
import { usePagination } from "@/hooks/usePagination";
import { useAuth, can } from "@/stores/auth";
import {
  formatCurrency,
  formatDuration,
  formatNumber,
  formatRelative,
  truncate,
} from "@/lib/format";
import type { Run, RunStatus, RunSummary } from "@/lib/types";

const STATUS_OPTIONS: Array<{ value: RunStatus; label: string }> = [
  { value: "running", label: "Running" },
  { value: "pending", label: "Pending" },
  { value: "succeeded", label: "Succeeded" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

function summaryOf(run: Run): RunSummary | null {
  if (run.summary && typeof run.summary === "object" && "status" in run.summary) {
    return run.summary as RunSummary;
  }
  return null;
}

export function RunsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const user = useAuth((state) => state.user);
  const canTrigger = can(user, "run:trigger");

  const pagination = usePagination(25);
  const [status, setStatus] = useState<RunStatus | "">("");
  const [dialogOpen, setDialogOpen] = useState(false);

  const runs = useRuns({
    page: pagination.page,
    pageSize: pagination.pageSize,
    status,
  });

  // The dashboard deep-links here with ?new=1 to open the trigger dialog.
  useEffect(() => {
    if (params.get("new") === "1" && canTrigger) {
      setDialogOpen(true);
      params.delete("new");
      setParams(params, { replace: true });
    }
  }, [params, setParams, canTrigger]);

  // Poll while any run is still in flight so the list stays honest without a
  // permanent background refresh.
  const anyActive = (runs.data?.items ?? []).some(
    (run) => run.status === "running" || run.status === "pending",
  );

  useEffect(() => {
    if (!anyActive) return;
    const timer = setInterval(() => void runs.refetch(), 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anyActive]);

  const columns: Array<Column<Run>> = [
    {
      key: "id",
      header: "Run ID",
      cell: (row) => (
        <span className="font-mono text-xs text-brand-300">{truncate(row.id, 22)}</span>
      ),
    },
    { key: "status", header: "Status", cell: (row) => <StatusPill domain="run" value={row.status} /> },
    {
      key: "trigger",
      header: "Trigger",
      cell: (row) => <Badge tone={row.trigger_type === "manual" ? "brand" : "neutral"}>{row.trigger_type}</Badge>,
    },
    {
      key: "scope",
      header: "Scope",
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {row.campaign_ids.length === 0 ? "all campaigns" : `${row.campaign_ids.length} campaigns`}
        </span>
      ),
    },
    {
      key: "iteration",
      header: "Iterations",
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {row.iteration} / {row.max_iterations}
        </span>
      ),
    },
    {
      key: "outcome",
      header: "Outcome",
      align: "right",
      cell: (row) => {
        const summary = summaryOf(row);
        if (!summary) return <span className="text-xs text-ink-3">—</span>;
        return (
          <span className="tnum text-xs text-ink-2">
            {formatNumber(summary.actions)} proposals · {formatNumber(summary.alerts_raised)} alerts ·{" "}
            {formatNumber(summary.creatives_generated)} creatives
          </span>
        );
      },
    },
    {
      key: "spend",
      header: "Model spend",
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {formatCurrency(row.llm_cost_usd, { digits: 4 })}
        </span>
      ),
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {row.started_at ? formatDuration(row.started_at, row.finished_at) : "—"}
        </span>
      ),
    },
    {
      key: "created",
      header: "Started",
      align: "right",
      cell: (row) => <span className="text-xs text-ink-3">{formatRelative(row.created_at)}</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Optimization runs"
        description="Each run is one supervisor execution. Open a run to watch the agents work in real time."
        actions={
          canTrigger && (
            <Button variant="primary" icon="play" onClick={() => setDialogOpen(true)}>
              New run
            </Button>
          )
        }
      />

      <Card padded={false}>
        <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
          <SelectField
            wrapClassName="w-44"
            label="Status"
            value={status}
            placeholder="All"
            options={STATUS_OPTIONS}
            onChange={(event) => {
              setStatus(event.target.value as RunStatus | "");
              pagination.reset();
            }}
          />
          <Button variant="ghost" icon="refresh" onClick={() => void runs.refetch()} loading={runs.isFetching}>
            Refresh
          </Button>
          {anyActive && (
            <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-ink-3">
              <span className="size-1.5 rounded-full bg-pos live-dot" />
              auto-refreshing every 5s while a run is in flight
            </span>
          )}
        </div>

        {runs.isError && (
          <div className="px-4 pt-3">
            <ErrorNotice error={runs.error} onRetry={() => void runs.refetch()} />
          </div>
        )}

        <Table<Run>
          columns={columns}
          rows={runs.data?.items ?? []}
          rowKey={(row) => row.id}
          loading={runs.isLoading}
          onRowClick={(row) => navigate(`/runs/${row.id}`)}
          emptyTitle="No runs recorded"
          emptyHint="Trigger the supervisor to analyse delivery and propose changes."
          skeletonRows={8}
        />

        <Pagination
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={runs.data?.total ?? 0}
          onChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />
      </Card>

      <RunTriggerDialog open={dialogOpen} onClose={() => setDialogOpen(false)} />
    </div>
  );
}