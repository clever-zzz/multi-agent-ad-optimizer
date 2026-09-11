import { useI18n } from "@/i18n";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/StatCard";
import { CampaignFormDialog } from "@/components/CampaignFormDialog";
import { RunTriggerDialog } from "@/components/RunTriggerDialog";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { Tabs } from "@/components/ui/Tabs";
import { Table, type Column } from "@/components/ui/Table";
import { StatusPill } from "@/components/ui/StatusPill";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { Skeleton } from "@/components/ui/Skeleton";
import { TrendChart, type TrendDatum, type TrendSeries } from "@/components/charts/TrendChart";
import { BarList } from "@/components/charts/BarList";
import { useCampaign, useCreatives } from "@/hooks/useCampaigns";
import { useCampaignBreakdown, useTimeseries } from "@/hooks/useAnalytics";
import { useActions } from "@/hooks/useActions";
import { useAlerts } from "@/hooks/useAlerts";
import { useAuth, can } from "@/stores/auth";
import {
  formatCurrency,
  formatDateTime,
  formatNumber,
  formatPercent,
  formatRatio,
  formatRelative,
  humanize,
  truncate,
} from "@/lib/format";
import type { Alert, Creative, OptimizationAction } from "@/lib/types";

const SERIES: TrendSeries[] = [
  { key: "cost", label: "Spend", color: "var(--color-brand-500)", kind: "bar" },
  { key: "revenue", label: "Revenue", color: "var(--color-pos)", kind: "area" },
  {
    key: "roas",
    label: "ROAS",
    color: "var(--color-accent-400)",
    axis: "right",
    format: (value) => `${value.toFixed(2)}x`,
  },
];

const TREND_WINDOWS = [
  { value: "14", label: "14d" },
  { value: "30", label: "30d" },
  { value: "90", label: "90d" },
];

export function CampaignDetailPage() {
  const { t } = useI18n();
  const { campaignId = "" } = useParams();
  const navigate = useNavigate();
  const user = useAuth((state) => state.user);
  const canWrite = can(user, "campaign:write");
  const canTrigger = can(user, "run:trigger");

  const [windowDays, setWindowDays] = useState("7");
  const [trendDays, setTrendDays] = useState("30");
  const [editOpen, setEditOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);

  const campaign = useCampaign(campaignId);
  const breakdown = useCampaignBreakdown(campaignId, Number(windowDays));
  const trend = useTimeseries(Number(trendDays), campaignId);
  const creatives = useCreatives(campaignId);
  const actions = useActions({ page: 1, pageSize: 10, campaignId });
  const alerts = useAlerts({ page: 1, pageSize: 10, campaignId, status: "open" });

  const snapshot = breakdown.data?.snapshot ?? null;
  const record = campaign.data;

  const trendData = useMemo<TrendDatum[]>(
    () =>
      (trend.data ?? []).map((point) => ({
        label: point.date.slice(5),
        values: { cost: point.cost, revenue: point.revenue, roas: point.roas },
      })),
    [trend.data],
  );

  const creativeNameById = useMemo(() => {
    const map = new Map<string, Creative>();
    for (const creative of creatives.data ?? []) map.set(creative.id, creative);
    return map;
  }, [creatives.data]);

  const creativeColumns: Array<Column<Creative>> = [
    {
      key: "headline",
      header: t("Creative"),
      cell: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink-1">{row.headline}</p>
          {row.description && (
            <p className="truncate text-[11px] text-ink-3">{truncate(row.description, 90)}</p>
          )}
        </div>
      ),
      className: "max-w-96",
    },
    { key: "type", header: t("Type"), cell: (row) => <Badge>{row.creative_type}</Badge> },
    { key: "status", header: t("Status"), cell: (row) => <StatusPill domain="creative" value={row.status} /> },
    { key: "group", header: t("Test group"), cell: (row) => <span className="text-xs text-ink-2">{row.ab_group}</span> },
    {
      key: "origin",
      header: t("Origin"),
      cell: (row) => (
        <Badge tone={row.origin === "llm" ? "violet" : row.origin === "rule" ? "info" : "neutral"}>
          {row.origin}
        </Badge>
      ),
    },
    {
      key: "score",
      header: t("Score"),
      align: "right",
      cell: (row) =>
        row.score === null ? (
          <span className="text-xs text-ink-3">{t("unscored")}</span>
        ) : (
          <span className="tnum text-xs font-semibold text-ink-1">{row.score.toFixed(1)}</span>
        ),
    },
    {
      key: "created",
      header: t("Created"),
      align: "right",
      cell: (row) => <span className="text-xs text-ink-3">{formatRelative(row.created_at)}</span>,
    },
  ];

  const actionColumns: Array<Column<OptimizationAction>> = [
    { key: "type", header: t("Proposal"), cell: (row) => <span className="text-xs text-ink-1">{humanize(row.action_type)}</span> },
    {
      key: "change",
      header: t("Change"),
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {row.before_value || "—"} → <span className="font-medium text-ink-1">{row.after_value || "—"}</span>
        </span>
      ),
    },
    {
      key: "confidence",
      header: t("Confidence"),
      align: "right",
      cell: (row) => <span className="tnum text-xs text-ink-2">{formatPercent(row.confidence, 0)}</span>,
    },
    { key: "status", header: t("Status"), cell: (row) => <StatusPill domain="action" value={row.status} /> },
    {
      key: "created",
      header: t("Proposed"),
      align: "right",
      cell: (row) => <span className="text-xs text-ink-3">{formatRelative(row.created_at)}</span>,
    },
  ];

  const alertColumns: Array<Column<Alert>> = [
    { key: "severity", header: t("Severity"), cell: (row) => <StatusPill domain="severity" value={row.severity} /> },
    { key: "rule", header: t("Rule"), cell: (row) => <span className="text-xs text-ink-1">{humanize(row.rule)}</span> },
    {
      key: "message",
      header: t("Detail"),
      cell: (row) => <span className="text-xs text-ink-2">{truncate(row.message, 110)}</span>,
      className: "max-w-md",
    },
    {
      key: "observed",
      header: t("Observed / threshold"),
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {row.observed === null ? "—" : row.observed.toFixed(3)} / {row.threshold.toFixed(3)}
        </span>
      ),
    },
    { key: "status", header: t("Status"), cell: (row) => <StatusPill domain="alert" value={row.status} /> },
  ];

  if (campaign.isError) {
    return (
      <div className="flex flex-col gap-4">
        <PageHeader title={t("Campaign")} breadcrumb={<Link to="/campaigns" className="hover:text-ink-1">{t("Campaigns")}</Link>} />
        <ErrorNotice error={campaign.error} onRetry={() => void campaign.refetch()} title={t("Campaign unavailable")} />
      </div>
    );
  }

  const roasTone = snapshot
    ? snapshot.roas >= (record?.target_roas ?? 2)
      ? "positive"
      : snapshot.roas >= 1
        ? "warning"
        : "negative"
    : "neutral";
  const cpaTone =
    snapshot && snapshot.cpa !== null
      ? snapshot.cpa <= (record?.target_cpa ?? Infinity)
        ? "positive"
        : "negative"
      : "neutral";

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        breadcrumb={
          <>
            <Link to="/campaigns" className="hover:text-ink-1">
              {t("Campaigns")}
            </Link>
            <Icon name="chevronRight" size={11} />
            <span className="text-ink-2">{record ? truncate(record.name, 40) : campaignId}</span>
          </>
        }
        title={record?.name ?? t("Loading campaign…")}
        description={
          record && (
            <span className="flex flex-wrap items-center gap-2">
              <StatusPill domain="platform" value={record.platform} dot={false} />
              <StatusPill domain="campaign" value={record.status} />
              <span className="tnum text-ink-3">
                {t("{objective} · started {date}", {
                  objective: humanize(record.objective),
                  date: formatDateTime(record.start_date),
                })}
                {record.end_date
                  ? t(" · ends {date}", { date: formatDateTime(record.end_date) })
                  : t(" · always-on")}
              </span>
            </span>
          )
        }
        actions={
          <>
            {canTrigger && (
              <Button variant="primary" icon="play" onClick={() => setRunOpen(true)}>
                {t("Optimize this campaign")}
              </Button>
            )}
            {canWrite && (
              <Button variant="secondary" icon="sliders" onClick={() => setEditOpen(true)}>
                {t("Edit")}
              </Button>
            )}
          </>
        }
      />

      {campaign.isLoading && <Skeleton className="h-24 w-full" />}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={t("Spend ({days}d)", { days: windowDays })}
          icon="wallet"
          tone="brand"
          loading={breakdown.isLoading}
          value={formatCurrency(snapshot?.total_cost, { compact: true })}
          hint={t("daily budget {amount}", { amount: formatCurrency(record?.daily_budget, { digits: 0 }) })}
        />
        <StatCard
          label={t("Revenue")}
          icon="trendUp"
          tone="positive"
          loading={breakdown.isLoading}
          value={formatCurrency(snapshot?.total_revenue, { compact: true })}
          hint={t("{count} conversions", { count: formatNumber(snapshot?.conversions) })}
        />
        <StatCard
          label={t("ROAS vs target")}
          icon="target"
          tone={roasTone}
          loading={breakdown.isLoading}
          value={formatRatio(snapshot?.roas)}
          hint={t("target {value}", { value: formatRatio(record?.target_roas) })}
        />
        <StatCard
          label={t("CPA vs target")}
          icon="users"
          tone={cpaTone}
          loading={breakdown.isLoading}
          value={formatCurrency(snapshot?.cpa)}
          hint={t("target {value}", { value: formatCurrency(record?.target_cpa) })}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label={t("CTR")} icon="eye" loading={breakdown.isLoading} value={formatPercent(snapshot?.ctr)} />
        <StatCard label={t("CVR")} icon="check" loading={breakdown.isLoading} value={formatPercent(snapshot?.cvr)} />
        <StatCard label={t("CPC")} icon="wallet" loading={breakdown.isLoading} value={formatCurrency(snapshot?.cpc)} />
        <StatCard
          label={t("CTR lower bound")}
          icon="shield"
          loading={breakdown.isLoading}
          value={formatPercent(snapshot?.ctr_lower_bound)}
          hint={t("Wilson 95% — used instead of raw CTR when volume is thin")}
        />
      </div>

      <Card
        title={t("Delivery trend")}
        subtitle={t("Daily spend, revenue and ROAS for this campaign")}
        actions={
          <Tabs size="sm" items={TREND_WINDOWS.map((w) => ({ value: w.value, label: w.label }))} value={trendDays} onChange={setTrendDays} />
        }
      >
        {trend.isLoading ? (
          <Skeleton className="h-56 w-full" />
        ) : (
          <TrendChart
            data={trendData}
            series={SERIES.map((series) => ({ ...series, label: t(series.label) }))}
            height={240}
            yLeftLabel="USD"
            yRightLabel="ROAS"
          />
        )}
      </Card>

      <div className="grid gap-3 lg:grid-cols-3">
        <Card
          className="lg:col-span-2"
          title={t("Creatives")}
          subtitle={t("{count} attached · scored on {days}-day delivery", {
            count: formatNumber(creatives.data?.length ?? 0),
            days: windowDays,
          })}
          padded={false}
          actions={
            <Tabs
              size="sm"
              items={[
                { value: "7", label: "7d" },
                { value: "14", label: "14d" },
                { value: "30", label: "30d" },
              ]}
              value={windowDays}
              onChange={setWindowDays}
            />
          }
        >
          <Table<Creative>
            columns={creativeColumns}
            rows={creatives.data ?? []}
            rowKey={(row) => row.id}
            loading={creatives.isLoading}
            emptyTitle={t("No creatives yet")}
            emptyHint={t("The creative agent generates variants during an optimization run, or you can add one manually.")}
            skeletonRows={4}
            dense
          />
        </Card>

        <Card title={t("Creative score leaderboard")} subtitle={t("Composite of volume, efficiency and revenue")}>
          {breakdown.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            <BarList
              dense
              items={(breakdown.data?.creatives ?? []).slice(0, 8).map((row) => {
                const creative = creativeNameById.get(row.creative_id);
                return {
                  id: row.creative_id,
                  label: creative ? truncate(creative.headline, 34) : truncate(row.creative_id, 20),
                  value: row.score,
                  tone: creative?.origin === "llm" ? ("violet" as const) : ("brand" as const),
                  hint: t("{impr} impr · {cost}", {
                    impr: formatNumber(row.impressions),
                    cost: formatCurrency(row.cost, { compact: true }),
                  }),
                };
              })}
              format={(value) => value.toFixed(1)}
              emptyLabel={t("No scored creatives in this window")}
            />
          )}
        </Card>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card
          title={t("Proposed actions")}
          subtitle={t("Latest proposals for this campaign")}
          padded={false}
          actions={
            <Link to="/actions">
              <Button size="xs" variant="ghost" iconRight="chevronRight">
                {t("Approval queue")}
              </Button>
            </Link>
          }
        >
          <Table<OptimizationAction>
            columns={actionColumns}
            rows={actions.data?.items ?? []}
            rowKey={(row) => row.id ?? `${row.action_type}-${row.created_at}`}
            loading={actions.isLoading}
            onRowClick={() => navigate("/actions")}
            emptyTitle={t("No proposals")}
            emptyHint={t("Run the optimizer to generate budget, bid and creative proposals.")}
            skeletonRows={3}
            dense
          />
        </Card>

        <Card
          title={t("Open alerts")}
          subtitle={t("Unresolved anomalies from the monitor agent")}
          padded={false}
          actions={
            <Link to="/alerts">
              <Button size="xs" variant="ghost" iconRight="chevronRight">
                {t("All alerts")}
              </Button>
            </Link>
          }
        >
          {alerts.data && alerts.data.items.length === 0 ? (
            <EmptyState tone="neutral" icon="check" title={t("No open alerts")} hint={t("Delivery is inside every configured threshold.")} />
          ) : (
            <Table<Alert>
              columns={alertColumns}
              rows={alerts.data?.items ?? []}
              rowKey={(row) => row.id ?? row.dedup_key}
              loading={alerts.isLoading}
              onRowClick={() => navigate("/alerts")}
              skeletonRows={3}
              dense
            />
          )}
        </Card>
      </div>

      <CampaignFormDialog open={editOpen} onClose={() => setEditOpen(false)} campaign={record} />
      <RunTriggerDialog open={runOpen} onClose={() => setRunOpen(false)} initialCampaignIds={[campaignId]} />
    </div>
  );
}