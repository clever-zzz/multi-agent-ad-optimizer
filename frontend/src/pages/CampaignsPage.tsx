import { useI18n } from "@/i18n";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { CampaignFormDialog } from "@/components/CampaignFormDialog";
import { RunTriggerDialog } from "@/components/RunTriggerDialog";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { SelectField } from "@/components/ui/Field";
import { Table, type Column } from "@/components/ui/Table";
import { Pagination } from "@/components/ui/Pagination";
import { StatusPill } from "@/components/ui/StatusPill";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { useCampaigns, useDeleteCampaign } from "@/hooks/useCampaigns";
import { useSnapshots } from "@/hooks/useAnalytics";
import { useDebounced } from "@/hooks/useDebounced";
import { usePagination } from "@/hooks/usePagination";
import { useAuth, can } from "@/stores/auth";
import { toast } from "@/stores/toast";
import {
  formatCurrency,
  formatNumber,
  formatPercent,
  formatRatio,
  truncate,
} from "@/lib/format";
import type { Campaign, CampaignStatus, PerformanceSnapshot, Platform } from "@/lib/types";

const PLATFORM_OPTIONS = [
  { value: "google", label: "Google" },
  { value: "meta", label: "Meta" },
  { value: "tiktok", label: "TikTok" },
  { value: "mock", label: "Mock" },
];

const STATUS_OPTIONS = [
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
  { value: "completed", label: "Completed" },
  { value: "archived", label: "Archived" },
];

const WINDOW_OPTIONS = [
  { value: "7", label: "7 days" },
  { value: "14", label: "14 days" },
  { value: "30", label: "30 days" },
];

export function CampaignsPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const user = useAuth((state) => state.user);
  const canWrite = can(user, "campaign:write");
  const canTrigger = can(user, "run:trigger");

  const pagination = usePagination(25);
  const [search, setSearch] = useState("");
  const [platform, setPlatform] = useState<Platform | "">("");
  const [status, setStatus] = useState<CampaignStatus | "">("");
  const [windowDays, setWindowDays] = useState("7");
  const debouncedSearch = useDebounced(search, 300);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Campaign | null>(null);
  const [runTarget, setRunTarget] = useState<string[] | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Campaign | null>(null);

  const campaigns = useCampaigns({
    page: pagination.page,
    pageSize: pagination.pageSize,
    platform,
    status,
    search: debouncedSearch,
  });
  const snapshots = useSnapshots(Number(windowDays));
  const deleteCampaign = useDeleteCampaign();

  const snapshotById = useMemo(() => {
    const map = new Map<string, PerformanceSnapshot>();
    for (const snapshot of snapshots.data ?? []) map.set(snapshot.campaign_id, snapshot);
    return map;
  }, [snapshots.data]);

  const openCreate = () => {
    setEditing(null);
    setFormOpen(true);
  };

  const openEdit = (campaign: Campaign) => {
    setEditing(campaign);
    setFormOpen(true);
  };

  const confirmDelete = () => {
    if (!deleteTarget) return;
    deleteCampaign.mutate(deleteTarget.id, {
      onSuccess: () => {
        toast.success(t("Campaign deleted"), deleteTarget.name);
        setDeleteTarget(null);
      },
      onError: (error) => {
        toast.error(t("Delete failed"), (error as Error).message);
      },
    });
  };

  const columns: Array<Column<Campaign>> = [
    {
      key: "name",
      header: t("Campaign"),
      cell: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink-1">{row.name}</p>
          <p className="tnum truncate font-mono text-[11px] text-ink-3">{truncate(row.id, 26)}</p>
        </div>
      ),
      className: "max-w-64",
    },
    {
      key: "platform",
      header: t("Platform"),
      cell: (row) => <StatusPill domain="platform" value={row.platform} />,
    },
    { key: "status", header: t("Status"), cell: (row) => <StatusPill domain="campaign" value={row.status} /> },
    {
      key: "daily_budget",
      header: t("Daily budget"),
      align: "right",
      cell: (row) => <span className="tnum text-xs">{formatCurrency(row.daily_budget, { digits: 0 })}</span>,
    },
    {
      key: "targets",
      header: t("Targets"),
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          CPA {formatCurrency(row.target_cpa, { digits: 0 })} · ROAS {row.target_roas.toFixed(1)}x
        </span>
      ),
    },
    {
      key: "spend",
      header: t("Spend ({days}d)", { days: windowDays }),
      align: "right",
      cell: (row) => {
        const snapshot = snapshotById.get(row.id);
        return (
          <span className="tnum text-xs">
            {snapshot ? formatCurrency(snapshot.total_cost, { compact: true }) : "—"}
          </span>
        );
      },
    },
    {
      key: "roas",
      header: t("ROAS"),
      align: "right",
      cell: (row) => {
        const snapshot = snapshotById.get(row.id);
        if (!snapshot) return <span className="text-xs text-ink-3">—</span>;
        const tone = snapshot.roas >= row.target_roas ? "text-pos" : snapshot.roas >= 1 ? "text-warn" : "text-neg";
        return <span className={`tnum text-xs font-semibold ${tone}`}>{formatRatio(snapshot.roas)}</span>;
      },
    },
    {
      key: "cpa",
      header: t("CPA"),
      align: "right",
      cell: (row) => {
        const snapshot = snapshotById.get(row.id);
        if (!snapshot) return <span className="text-xs text-ink-3">—</span>;
        const tone = snapshot.cpa !== null && snapshot.cpa <= row.target_cpa ? "text-pos" : "text-neg";
        return (
          <span className={`tnum text-xs ${tone}`}>{formatCurrency(snapshot.cpa)}</span>
        );
      },
    },
    {
      key: "ctr",
      header: t("CTR"),
      align: "right",
      cell: (row) => {
        const snapshot = snapshotById.get(row.id);
        return <span className="tnum text-xs text-ink-2">{snapshot ? formatPercent(snapshot.ctr) : "—"}</span>;
      },
    },
    {
      key: "conversions",
      header: t("Conv"),
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {snapshotById.get(row.id) ? formatNumber(snapshotById.get(row.id)?.conversions ?? 0) : "—"}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (row) => (
        <div className="flex items-center justify-end gap-1">
          {canTrigger && (
            <Button
              size="xs"
              variant="ghost"
              icon="play"
              title={t("Run optimization for this campaign")}
              aria-label={t("Run optimization for {name}", { name: row.name })}
              onClick={() => setRunTarget([row.id])}
            />
          )}
          {canWrite && (
            <>
              <Button
                size="xs"
                variant="ghost"
                icon="sliders"
                title={t("Edit campaign")}
                aria-label={t("Edit {name}", { name: row.name })}
                onClick={() => openEdit(row)}
              />
              <Button
                size="xs"
                variant="ghost"
                icon="trash"
                title={t("Delete campaign")}
                aria-label={t("Delete {name}", { name: row.name })}
                className="text-neg hover:bg-neg/10"
                onClick={() => setDeleteTarget(row)}
              />
            </>
          )}
          <Button
            size="xs"
            variant="ghost"
            iconRight="chevronRight"
            title={t("Open detail")}
            aria-label={t("Open {name}", { name: row.name })}
            onClick={() => navigate(`/campaigns/${row.id}`)}
          />
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("Campaigns")}
        description={t("Configuration on the left, live delivery on the right. Performance columns use the selected lookback window.")}
        actions={
          <>
            {canTrigger && (
              <Button variant="secondary" icon="play" onClick={() => setRunTarget([])}>
                {t("Optimize all")}
              </Button>
            )}
            {canWrite && (
              <Button variant="primary" icon="plus" onClick={openCreate}>
                {t("New campaign")}
              </Button>
            )}
          </>
        }
      />

      <Card padded={false}>
        <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
          <div className="relative min-w-52 flex-1">
            <Icon
              name="search"
              size={14}
              className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-ink-3"
            />
            <input
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                pagination.reset();
              }}
              placeholder={t("Search by name or external ID…")}
              aria-label={t("Search campaigns")}
              className="field pl-8"
            />
          </div>
          <SelectField
            wrapClassName="w-36"
            label={t("Platform")}
            value={platform}
            placeholder={t("All")}
            options={PLATFORM_OPTIONS}
            onChange={(event) => {
              setPlatform(event.target.value as Platform | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-36"
            label={t("Status")}
            value={status}
            placeholder={t("All")}
            options={STATUS_OPTIONS}
            onChange={(event) => {
              setStatus(event.target.value as CampaignStatus | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-36"
            label={t("Window")}
            value={windowDays}
            options={WINDOW_OPTIONS}
            onChange={(event) => setWindowDays(event.target.value)}
          />
          {(search || platform || status) && (
            <Button
              variant="ghost"
              icon="close"
              onClick={() => {
                setSearch("");
                setPlatform("");
                setStatus("");
                pagination.reset();
              }}
            >
              {t("Clear")}
            </Button>
          )}
        </div>

        {campaigns.isError && (
          <div className="px-4 pt-3">
            <ErrorNotice error={campaigns.error} onRetry={() => void campaigns.refetch()} />
          </div>
        )}

        <Table<Campaign>
          columns={columns}
          rows={campaigns.data?.items ?? []}
          rowKey={(row) => row.id}
          loading={campaigns.isFetching}
          onRowClick={(row) => navigate(`/campaigns/${row.id}`)}
          emptyTitle={t("No campaigns match")}
          emptyHint={
            canWrite
              ? t("Create one, or load the deterministic demo dataset from the System page.")
              : t("Ask an administrator to create a campaign or seed the demo dataset.")
          }
          skeletonRows={8}
        />

        <Pagination
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={campaigns.data?.total ?? 0}
          onChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />
      </Card>

      <CampaignFormDialog
        open={formOpen}
        onClose={() => setFormOpen(false)}
        campaign={editing}
      />
      <RunTriggerDialog
        open={runTarget !== null}
        onClose={() => setRunTarget(null)}
        initialCampaignIds={runTarget ?? undefined}
      />
      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("Delete campaign")}
        tone="danger"
        busy={deleteCampaign.isPending}
        confirmLabel="Delete permanently"
        message={
          <>
            <p>
              <span className="font-semibold text-ink-1">{deleteTarget?.name}</span>{" "}
              {t("and its creatives, metrics and proposed actions will be removed.")}
            </p>
            <p className="mt-2 text-xs text-ink-3">
              {t("Runs and audit entries referencing it are retained for compliance. This cannot be undone.")}
            </p>
          </>
        }
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}