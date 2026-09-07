import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/StatCard";
import { ActionItem } from "@/components/ActionItem";
import { RunTimeline } from "@/components/RunTimeline";
import { fromRunEvent, fromStreamFrame, type TimelineItem } from "@/lib/timeline";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { Tabs } from "@/components/ui/Tabs";
import { Table, type Column } from "@/components/ui/Table";
import { StatusPill } from "@/components/ui/StatusPill";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { useRun, useRunStream, useCancelRun } from "@/hooks/useRuns";
import { useApproveAction, useExecuteAction, useRejectAction } from "@/hooks/useActions";
import { useCampaignNames } from "@/hooks/useCampaigns";
import { useAuth, can } from "@/stores/auth";
import { toast } from "@/stores/toast";
import {
  formatCurrency,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatSignedPercent,
  humanize,
  truncate,
} from "@/lib/format";
import type { BudgetAllocation, OptimizationAction, RunSummary } from "@/lib/types";

type TabValue = "proposals" | "budget" | "usage";

function summaryOf(summary: RunSummary | Record<string, unknown> | undefined): RunSummary | null {
  if (summary && typeof summary === "object" && "status" in summary) return summary as RunSummary;
  return null;
}

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const user = useAuth((state) => state.user);
  const canApprove = can(user, "action:approve");
  const canExecute = can(user, "action:execute");
  const canTrigger = can(user, "run:trigger");

  const [tab, setTab] = useState<TabValue>("proposals");
  const [cancelOpen, setCancelOpen] = useState(false);
  const [rejectTarget, setRejectTarget] = useState<OptimizationAction | null>(null);

  const campaignNames = useCampaignNames();
  const detail = useRun(runId, { refetchMs: 8000 });
  const cancelRun = useCancelRun();
  const approveAction = useApproveAction();
  const rejectAction = useRejectAction();
  const executeAction = useExecuteAction();

  const run = detail.data?.run;
  const inFlight = run?.status === "running" || run?.status === "pending";
  const stream = useRunStream(runId, Boolean(inFlight));

  // Live frames take precedence while streaming; persisted events are the record
  // of truth once the run has terminated.
  const timeline = useMemo<TimelineItem[]>(() => {
    if (stream.frames.length > 0) return stream.frames.map(fromStreamFrame);
    return (detail.data?.events ?? []).map(fromRunEvent);
  }, [stream.frames, detail.data]);

  const summary = summaryOf(run?.summary);
  const actions = detail.data?.actions ?? [];
  const allocations = detail.data?.allocations ?? [];

  const handleApprove = (action: OptimizationAction, execute: boolean) => {
    if (!action.id) return;
    approveAction.mutate(
      { actionId: action.id, execute },
      {
        onSuccess: (updated) => {
          toast.success(
            execute ? "Approved and executed" : "Approved",
            `${humanize(updated.action_type)} is now ${updated.status}.`,
          );
        },
        onError: (error) => toast.error("Approval failed", (error as Error).message),
      },
    );
  };

  const handleReject = () => {
    if (!rejectTarget?.id) return;
    rejectAction.mutate(
      { actionId: rejectTarget.id, reason: "" },
      {
        onSuccess: () => {
          toast.info("Proposal rejected", humanize(rejectTarget.action_type));
          setRejectTarget(null);
        },
        onError: (error) => toast.error("Rejection failed", (error as Error).message),
      },
    );
  };

  const allocationColumns: Array<Column<BudgetAllocation>> = [
    {
      key: "campaign",
      header: "Campaign",
      cell: (row) => (
        <Link to={`/campaigns/${row.campaign_id}`} className="text-[13px] text-ink-1 hover:text-brand-300 hover:underline">
          {row.campaign_name || truncate(row.campaign_id, 26)}
        </Link>
      ),
    },
    {
      key: "current",
      header: "Current",
      align: "right",
      cell: (row) => <span className="tnum text-xs text-ink-2">{formatCurrency(row.current_budget, { digits: 0 })}</span>,
    },
    {
      key: "recommended",
      header: "Recommended",
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs font-semibold text-ink-1">
          {formatCurrency(row.recommended_budget, { digits: 0 })}
        </span>
      ),
    },
    {
      key: "delta",
      header: "Δ",
      align: "right",
      cell: (row) => (
        <span
          className={
            row.change_pct > 0.001
              ? "tnum text-xs font-semibold text-pos"
              : row.change_pct < -0.001
                ? "tnum text-xs font-semibold text-neg"
                : "tnum text-xs text-ink-3"
          }
        >
          {formatSignedPercent(row.change_pct)}
        </span>
      ),
    },
    {
      key: "score",
      header: "Score",
      align: "right",
      cell: (row) => <span className="tnum text-xs text-ink-2">{row.score.toFixed(2)}</span>,
    },
    { key: "solver", header: "Solver", cell: (row) => <Badge>{row.solver}</Badge> },
    {
      key: "reason",
      header: "Rationale",
      cell: (row) => <span className="text-xs text-ink-3">{truncate(row.reason, 90)}</span>,
      className: "max-w-sm",
    },
  ];

  if (detail.isError) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title="Run" breadcrumb={<Link to="/runs" className="hover:text-ink-1">Runs</Link>} />
        <ErrorNotice error={detail.error} onRetry={() => void detail.refetch()} title="Run unavailable" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        breadcrumb={
          <>
            <Link to="/runs" className="hover:text-ink-1">
              Runs
            </Link>
            <Icon name="chevronRight" size={11} />
            <span className="font-mono text-ink-2">{truncate(runId, 24)}</span>
          </>
        }
        title={run ? `Optimization run ${truncate(run.id, 20)}` : "Loading run…"}
        description={
          run && (
            <span className="flex flex-wrap items-center gap-2">
              <StatusPill domain="run" value={run.status} />
              <Badge tone={run.trigger_type === "manual" ? "brand" : "neutral"}>{run.trigger_type}</Badge>
              <span className="tnum text-ink-3">
                iteration {run.iteration}/{run.max_iterations} ·{" "}
                {run.campaign_ids.length === 0 ? "all campaigns" : `${run.campaign_ids.length} in scope`} ·
                started {formatDateTime(run.started_at ?? run.created_at)}
                {run.finished_at ? ` · ran ${formatDuration(run.started_at, run.finished_at)}` : ""}
              </span>
            </span>
          )
        }
        actions={
          <>
            {inFlight && canTrigger && (
              <Button variant="danger" icon="stop" onClick={() => setCancelOpen(true)}>
                Cancel run
              </Button>
            )}
            <Button variant="ghost" icon="refresh" onClick={() => void detail.refetch()} loading={detail.isFetching}>
              Refresh
            </Button>
          </>
        }
      />

      {run?.error_message && (
        <div className="flex items-start gap-2.5 rounded-lg border border-neg/35 bg-neg/8 px-3.5 py-3">
          <Icon name="warning" size={16} className="mt-0.5 shrink-0 text-neg" />
          <div>
            <p className="text-[13px] font-semibold text-ink-1">Run failed</p>
            <p className="mt-0.5 font-mono text-xs break-words text-ink-2">{run.error_message}</p>
          </div>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <StatCard
          label="Proposals"
          icon="checkSquare"
          tone={(summary?.actions ?? 0) > 0 ? "brand" : "neutral"}
          loading={detail.isLoading}
          value={formatNumber(summary?.actions ?? actions.length)}
          hint={summary ? `${formatNumber(summary.budget_adjustments)} budget moves` : undefined}
        />
        <StatCard
          label="Alerts raised"
          icon="bell"
          tone={(summary?.alerts_raised ?? 0) > 0 ? "warning" : "neutral"}
          loading={detail.isLoading}
          value={formatNumber(summary?.alerts_raised ?? 0)}
          hint="anomalies detected"
        />
        <StatCard
          label="Creatives generated"
          icon="sparkles"
          loading={detail.isLoading}
          value={formatNumber(summary?.creatives_generated ?? 0)}
          hint={`${formatNumber(summary?.bidding_decisions ?? 0)} bid decisions`}
        />
        <StatCard
          label="Model spend"
          icon="wallet"
          loading={detail.isLoading}
          value={formatCurrency(run?.llm_cost_usd, { digits: 4 })}
          hint={`${formatNumber((run?.prompt_tokens ?? 0) + (run?.completion_tokens ?? 0))} tokens`}
        />
        <StatCard
          label="Duration"
          icon="clock"
          loading={detail.isLoading}
          value={run?.started_at ? formatDuration(run.started_at, run.finished_at) : "—"}
          hint={run?.finished_at ? `finished ${formatDateTime(run.finished_at)}` : "in progress"}
        />
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        <Card
          title="Agent timeline"
          subtitle={
            inFlight
              ? "Streaming live over server-sent events"
              : `${formatNumber(timeline.length)} recorded events`
          }
          actions={
            <span className="inline-flex items-center gap-1.5 text-[11px]">
              <span
                className={
                  stream.connected
                    ? "size-1.5 rounded-full bg-pos live-dot"
                    : inFlight
                      ? "size-1.5 rounded-full bg-warn"
                      : "size-1.5 rounded-full bg-ink-3"
                }
              />
              <span className="text-ink-3">
                {stream.connected ? "live" : inFlight ? "reconnecting" : "finished"}
              </span>
            </span>
          }
          className="xl:sticky xl:top-18 xl:self-start"
          bodyClassName="max-h-[70vh] overflow-y-auto"
        >
          {stream.error && (
            <p className="mb-3 flex items-center gap-1.5 text-xs text-warn">
              <Icon name="warning" size={12} />
              Stream interrupted: {stream.error}. Showing persisted events.
            </p>
          )}
          {detail.isLoading ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <RunTimeline
              items={timeline}
              live={inFlight}
              emptyLabel={inFlight ? "Waiting for the first agent event…" : "No events recorded"}
            />
          )}
        </Card>

        <div className="flex min-w-0 flex-col gap-3">
          <Tabs<TabValue>
            value={tab}
            onChange={setTab}
            items={[
              { value: "proposals", label: "Proposals", count: actions.length },
              { value: "budget", label: "Budget plan", count: allocations.length },
              { value: "usage", label: "Summary & usage" },
            ]}
          />

          {tab === "proposals" && (
            <div className="flex flex-col gap-2.5">
              {detail.isLoading ? (
                <Skeleton className="h-40 w-full" />
              ) : actions.length === 0 ? (
                <Card>
                  <EmptyState
                    icon="checkSquare"
                    title="No proposals from this run"
                    hint="The optimizer only proposes changes when the evidence clears its thresholds. A clean run is a valid outcome."
                  />
                </Card>
              ) : (
                actions.map((action) => (
                  <ActionItem
                    key={action.id ?? `${action.action_type}-${action.campaign_id}`}
                    action={action}
                    campaignName={campaignNames.get(action.campaign_id)}
                    canApprove={canApprove}
                    canExecute={canExecute}
                    busy={approveAction.isPending || rejectAction.isPending || executeAction.isPending}
                    onApprove={(execute) => handleApprove(action, execute)}
                    onReject={() => setRejectTarget(action)}
                    onExecute={() =>
                      action.id &&
                      executeAction.mutate(action.id, {
                        onSuccess: () => toast.success("Executed", humanize(action.action_type)),
                        onError: (error) => toast.error("Execution failed", (error as Error).message),
                      })
                    }
                  />
                ))
              )}
            </div>
          )}

          {tab === "budget" && (
            <Card title="Budget reallocation plan" subtitle="Exact greedy allocation under the total budget constraint" padded={false}>
              <Table<BudgetAllocation>
                columns={allocationColumns}
                rows={allocations}
                rowKey={(row) => row.campaign_id}
                loading={detail.isLoading}
                emptyTitle="No budget changes proposed"
                emptyHint="Reallocation only triggers when score differences justify moving money."
                dense
              />
            </Card>
          )}

          {tab === "usage" && (
            <div className="flex flex-col gap-3">
              <Card title="Run summary" subtitle="Persisted with the run when it terminates">
                {summary ? (
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-3">
                    {[
                      ["Status", summary.status],
                      ["Iterations", String(summary.iterations)],
                      ["Campaigns analysed", String(summary.campaigns)],
                      ["Creatives generated", String(summary.creatives_generated)],
                      ["Bid decisions", String(summary.bidding_decisions)],
                      ["Budget adjustments", String(summary.budget_adjustments)],
                      ["Proposals", String(summary.actions)],
                      ["Alerts raised", String(summary.alerts_raised)],
                      ["Health score", String((summary.health as { score?: number }).score ?? "—")],
                    ].map(([label, value]) => (
                      <div key={label}>
                        <dt className="text-[11px] tracking-wide text-ink-3 uppercase">{label}</dt>
                        <dd className="tnum mt-0.5 text-sm font-semibold text-ink-1">{value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="text-xs text-ink-3">Run has not produced a summary yet.</p>
                )}

                {summary && Object.keys(summary.action_counts).length > 0 && (
                  <div className="mt-4 border-t border-line pt-3">
                    <p className="mb-2 text-[11px] tracking-wide text-ink-3 uppercase">
                      Proposals by type
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {Object.entries(summary.action_counts).map(([type, count]) => (
                        <Badge key={type} tone="brand">
                          {humanize(type)} · {count}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
              </Card>

              <Card title="Model usage" subtitle="Token consumption attributed to this run">
                <dl className="grid grid-cols-3 gap-4">
                  {[
                    ["Prompt tokens", formatNumber(run?.prompt_tokens ?? 0)],
                    ["Completion tokens", formatNumber(run?.completion_tokens ?? 0)],
                    ["Cost", formatCurrency(run?.llm_cost_usd ?? 0, { digits: 4 })],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-[11px] tracking-wide text-ink-3 uppercase">{label}</dt>
                      <dd className="tnum mt-0.5 text-lg font-semibold text-ink-1">{value}</dd>
                    </div>
                  ))}
                </dl>
                <p className="mt-3 border-t border-line pt-3 text-xs leading-relaxed text-ink-3">
                  Spend is recorded per gateway call in the LLM ledger, so the monthly budget
                  guardrail sees every retry and fallback, not just the successful ones.
                </p>
              </Card>

              {run && (
                <Card title="Run parameters" subtitle="As submitted by the operator or scheduler">
                  <pre className="overflow-x-auto rounded-lg border border-line bg-surface-2 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
                    {JSON.stringify(
                      {
                        trigger_type: run.trigger_type,
                        requested_by: run.requested_by,
                        campaign_ids: run.campaign_ids,
                        parameters: run.parameters,
                        max_iterations: run.max_iterations,
                      },
                      null,
                      2,
                    )}
                  </pre>
                </Card>
              )}
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={cancelOpen}
        title="Cancel this run"
        tone="danger"
        busy={cancelRun.isPending}
        confirmLabel="Cancel run"
        message={
          <>
            <p>The supervisor stops after the current agent step.</p>
            <p className="mt-2 text-xs text-ink-3">
              Proposals already persisted stay in the approval queue; nothing is executed by
              cancelling.
            </p>
          </>
        }
        onConfirm={() =>
          cancelRun.mutate(runId, {
            onSuccess: () => {
              toast.info("Run cancelled");
              setCancelOpen(false);
            },
            onError: (error) => toast.error("Cancel failed", (error as Error).message),
          })
        }
        onCancel={() => setCancelOpen(false)}
      />

      <ConfirmDialog
        open={rejectTarget !== null}
        title="Reject proposal"
        tone="danger"
        busy={rejectAction.isPending}
        confirmLabel="Reject"
        message={
          <p>
            <span className="font-semibold text-ink-1">
              {rejectTarget ? humanize(rejectTarget.action_type) : ""}
            </span>{" "}
            will be marked rejected and cannot be executed. The decision is written to the audit
            trail with your identity.
          </p>
        }
        onConfirm={handleReject}
        onCancel={() => setRejectTarget(null)}
      />
    </div>
  );
}