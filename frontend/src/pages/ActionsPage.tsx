import { useI18n } from "@/i18n";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { ActionItem } from "@/components/ActionItem";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { SelectField } from "@/components/ui/Field";
import { Tabs } from "@/components/ui/Tabs";
import { Pagination } from "@/components/ui/Pagination";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { useActions, useApproveAction, useBulkActions, useExecuteAction, useRejectAction } from "@/hooks/useActions";
import { useCampaignNames } from "@/hooks/useCampaigns";
import { usePagination } from "@/hooks/usePagination";
import { useAuth, can } from "@/stores/auth";
import { toast } from "@/stores/toast";
import { formatNumber, formatPercent, humanize } from "@/lib/format";
import type { ActionStatus, ActionType, OptimizationAction } from "@/lib/types";

type TabValue = "proposed" | "approved" | "executed" | "rejected" | "all";

const STATUS_TABS: Array<{ value: TabValue; label: string }> = [
  { value: "proposed", label: "Awaiting approval" },
  { value: "approved", label: "Approved" },
  { value: "executed", label: "Executed" },
  { value: "rejected", label: "Rejected" },
  { value: "all", label: "All" },
];

const TYPE_OPTIONS: Array<{ value: ActionType; label: string }> = [
  { value: "adjust_budget", label: "Adjust budget" },
  { value: "adjust_bid", label: "Adjust bid" },
  { value: "pause_creative", label: "Pause creative" },
  { value: "resume_creative", label: "Resume creative" },
  { value: "refresh_creative", label: "Refresh creative" },
  { value: "pause_campaign", label: "Pause campaign" },
  { value: "resume_campaign", label: "Resume campaign" },
  { value: "expand_audience", label: "Expand audience" },
  { value: "start_ab_test", label: "Start A/B test" },
  { value: "stop_ab_test", label: "Stop A/B test" },
];

const CONFIDENCE_OPTIONS = [
  { value: "0", label: "Any confidence" },
  { value: "0.5", label: "≥ 50%" },
  { value: "0.7", label: "≥ 70%" },
  { value: "0.85", label: "≥ 85%" },
];

export function ActionsPage() {
  const { t } = useI18n();
  const user = useAuth((state) => state.user);
  const canApprove = can(user, "action:approve");
  const canExecute = can(user, "action:execute");

  const pagination = usePagination(20);
  const [tab, setTab] = useState<TabValue>("proposed");
  const [type, setType] = useState<ActionType | "">("");
  const [minConfidence, setMinConfidence] = useState("0");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkConfirm, setBulkConfirm] = useState<"approve" | "execute" | "reject" | null>(null);

  const campaignNames = useCampaignNames();
  const actions = useActions({
    page: pagination.page,
    pageSize: pagination.pageSize,
    status: tab === "all" ? "" : (tab as ActionStatus),
    type,
    minConfidence: Number(minConfidence),
  });
  const approveAction = useApproveAction();
  const rejectAction = useRejectAction();
  const executeAction = useExecuteAction();
  const bulk = useBulkActions();

  // Memoised so the empty-array fallback keeps a stable identity. Without this the
  // useMemo below would see a brand-new array on every render while the query is
  // still in flight and recompute the selectable ids each time.
  const rows = useMemo(() => actions.data?.items ?? [], [actions.data]);

  useEffect(() => {
    setSelected(new Set());
  }, [tab, type, minConfidence, pagination.page, pagination.pageSize]);

  const selectableIds = useMemo(
    () => rows.filter((row) => row.status === "proposed" && row.id).map((row) => row.id as string),
    [rows],
  );
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));

  const toggle = (id: string, value: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (value) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const handleApprove = (action: OptimizationAction, execute: boolean) => {
    if (!action.id) return;
    approveAction.mutate(
      { actionId: action.id, execute },
      {
        onSuccess: (updated) =>
          toast.success(
            execute ? t("Approved and executed") : t("Approved"),
            t("{type} → {status}", {
              type: humanize(updated.action_type),
              status: humanize(updated.status),
            }),
          ),
        onError: (error) => toast.error(t("Approval failed"), (error as Error).message),
      },
    );
  };

  const handleReject = (action: OptimizationAction) => {
    if (!action.id) return;
    rejectAction.mutate(
      { actionId: action.id, reason: "" },
      {
        onSuccess: () => toast.info(t("Proposal rejected"), humanize(action.action_type)),
        onError: (error) => toast.error(t("Rejection failed"), (error as Error).message),
      },
    );
  };

  const runBulk = () => {
    const ids = [...selected];
    if (ids.length === 0 || !bulkConfirm) return;
    bulk.mutate(
      { actionIds: ids, execute: bulkConfirm === "execute", minConfidence: Number(minConfidence) },
      {
        onSuccess: (result) => {
          toast.success(
            t("Processed {count} proposals", { count: formatNumber(ids.length) }),
            t("{approved} approved · {executed} executed · {skipped} skipped · {failed} failed", {
              approved: result.approved.length,
              executed: result.executed.length,
              skipped: result.skipped.length,
              failed: result.failed.length,
            }),
          );
          setSelected(new Set());
          setBulkConfirm(null);
        },
        onError: (error) => {
          toast.error(t("Bulk operation failed"), (error as Error).message);
          setBulkConfirm(null);
        },
      },
    );
  };

  const busy = bulk.isPending || approveAction.isPending || rejectAction.isPending || executeAction.isPending;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("Action approvals")}
        description={t("The human-in-the-loop gate. The optimizer proposes; nothing reaches an ad platform until an operator approves, and every decision is written to the audit trail.")}
        actions={
          <Link to="/runs">
            <Button variant="secondary" icon="activity">
              {t("View runs")}
            </Button>
          </Link>
        }
      />

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-brand-600/25 bg-brand-600/8 px-3.5 py-2.5">
        <Icon name="shield" size={15} className="shrink-0 text-brand-300" />
        <p className="text-xs leading-relaxed text-ink-2">
          {t("Approval is enforced server-side by")}{" "}
          <code className="rounded bg-surface-3 px-1 py-0.5 font-mono text-[11px] text-brand-300">
            SECURITY__REQUIRE_ACTION_APPROVAL
          </code>
          {t(". Disabling it in a non-production environment lets the optimizer execute directly — the API rejects execution without approval whenever the flag is on.")}
        </p>
      </div>

      <Card padded={false}>
        <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
          <Tabs<TabValue>
            size="sm"
            items={STATUS_TABS}
            value={tab}
            onChange={(value) => {
              setTab(value);
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-44"
            label={t("Type")}
            value={type}
            placeholder={t("All types")}
            options={TYPE_OPTIONS}
            onChange={(event) => {
              setType(event.target.value as ActionType | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-40"
            label={t("Confidence")}
            value={minConfidence}
            options={CONFIDENCE_OPTIONS}
            onChange={(event) => {
              setMinConfidence(event.target.value);
              pagination.reset();
            }}
          />
          <Button variant="ghost" icon="refresh" onClick={() => void actions.refetch()} loading={actions.isFetching}>
            {t("Refresh")}
          </Button>
        </div>

        {actions.isError && (
          <div className="px-4 pt-3">
            <ErrorNotice error={actions.error} onRetry={() => void actions.refetch()} />
          </div>
        )}

        {canApprove && selectableIds.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-line bg-surface-2/50 px-4 py-2.5">
            <label className="flex items-center gap-2 text-xs text-ink-2">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={(event) =>
                  setSelected(event.target.checked ? new Set(selectableIds) : new Set())
                }
                className="size-3.5 accent-[var(--color-brand-500)]"
              />
              Select all {formatNumber(selectableIds.length)} pending on this page
            </label>

            {selected.size > 0 && (
              <>
                <span className="tnum text-xs font-medium text-brand-300">{t("{count} selected", { count: selected.size })}</span>
                <div className="ml-auto flex flex-wrap items-center gap-2">
                  <Button size="xs" variant="success" icon="check" onClick={() => setBulkConfirm("approve")} disabled={busy}>
                    {t("Approve")}
                  </Button>
                  {canExecute && (
                    <Button size="xs" variant="primary" icon="play" onClick={() => setBulkConfirm("execute")} disabled={busy}>
                      {t("Approve & execute")}
                    </Button>
                  )}
                  <Button
                    size="xs"
                    variant="ghost"
                    icon="close"
                    className="text-neg hover:bg-neg/10"
                    onClick={() => setBulkConfirm("reject")}
                    disabled={busy}
                  >
                    {t("Reject")}
                  </Button>
                </div>
              </>
            )}
          </div>
        )}

        <div className="flex flex-col gap-2.5 p-4">
          {actions.isLoading ? (
            Array.from({ length: 3 }).map((_, index) => <Skeleton key={index} className="h-36 w-full" />)
          ) : rows.length === 0 ? (
            <EmptyState
              icon="checkSquare"
              title={tab === "proposed" ? t("Approval queue is empty") : t("Nothing matches these filters")}
              hint={
                tab === "proposed"
                  ? t("Run the optimizer to generate proposals. A clean portfolio legitimately produces none.")
                  : t("Widen the status, type or confidence filters.")
              }
            />
          ) : (
            rows.map((action) => (
              <ActionItem
                key={action.id ?? `${action.action_type}-${action.campaign_id}`}
                action={action}
                campaignName={campaignNames.get(action.campaign_id)}
                canApprove={canApprove}
                canExecute={canExecute}
                busy={busy}
                selected={Boolean(action.id && selected.has(action.id))}
                onSelect={(value) => action.id && toggle(action.id, value)}
                onApprove={(execute) => handleApprove(action, execute)}
                onReject={() => handleReject(action)}
                onExecute={() =>
                  action.id &&
                  executeAction.mutate(action.id, {
                    onSuccess: () => toast.success(t("Executed"), humanize(action.action_type)),
                    onError: (error) => toast.error(t("Execution failed"), (error as Error).message),
                  })
                }
              />
            ))
          )}
        </div>

        <Pagination
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={actions.data?.total ?? 0}
          onChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
          pageSizeOptions={[10, 20, 50, 100]}
        />
      </Card>

      <ConfirmDialog
        open={bulkConfirm !== null}
        busy={bulk.isPending}
        tone={bulkConfirm === "reject" ? "danger" : bulkConfirm === "execute" ? "primary" : "success"}
        title={
          bulkConfirm === "approve"
            ? t("Approve proposals")
            : bulkConfirm === "execute"
              ? t("Approve and execute proposals")
              : t("Reject proposals")
        }
        confirmLabel={
          bulkConfirm === "approve"
            ? t("Approve all")
            : bulkConfirm === "execute"
              ? t("Approve & execute")
              : t("Reject all")
        }
        message={
          <>
            <p>
              {t("{count} proposals at or above {confidence} confidence.", {
                count: formatNumber(selected.size),
                confidence: formatPercent(Number(minConfidence), 0),
              })}
            </p>
            {bulkConfirm === "execute" && (
              <p className="mt-2 text-xs text-warn">
                {t(
                  "Execution calls the live platform adapter for each approved proposal. In mock data mode the adapter records the change without contacting a real network.",
                )}
              </p>
            )}
            {bulkConfirm === "reject" && (
              <p className="mt-2 text-xs text-ink-3">
                {t(
                  "Rejected proposals are closed permanently and recorded against your identity in the audit trail.",
                )}
              </p>
            )}
          </>
        }
        onConfirm={runBulk}
        onCancel={() => setBulkConfirm(null)}
      />
    </div>
  );
}