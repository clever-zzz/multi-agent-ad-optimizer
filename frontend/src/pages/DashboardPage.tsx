import { useI18n } from "@/i18n";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/StatCard";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { Tabs } from "@/components/ui/Tabs";
import { Table, type Column } from "@/components/ui/Table";
import { StatusPill } from "@/components/ui/StatusPill";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { Skeleton } from "@/components/ui/Skeleton";
import { TrendChart, type TrendDatum, type TrendSeries } from "@/components/charts/TrendChart";
import { BarList } from "@/components/charts/BarList";
import { Donut } from "@/components/charts/Donut";
import { useOverview, useTimeseries } from "@/hooks/useAnalytics";
import { useRuns } from "@/hooks/useRuns";
import { useAuth, can } from "@/stores/auth";
import {
  formatCompact,
  formatCurrency,
  formatNumber,
  formatPercent,
  formatRatio,
  formatRelative,
  truncate,
} from "@/lib/format";
import type { Run } from "@/lib/types";

const WINDOWS = [
  { value: "7", label: "7d" },
  { value: "14", label: "14d" },
  { value: "30", label: "30d" },
] as const;

type WindowValue = (typeof WINDOWS)[number]["value"];

const TREND_SERIES: TrendSeries[] = [
  { key: "cost", label: "Spend", color: "var(--color-brand-500)", kind: "bar" },
  { key: "revenue", label: "Revenue", color: "var(--color-pos)", kind: "area" },
  {
    key: "roas",
    label: "ROAS",
    color: "var(--color-accent-400)",
    axis: "right",
    kind: "line",
    format: (value) => `${value.toFixed(2)}x`,
  },
];

export function DashboardPage() {
  const navigate = useNavigate();
  const user = useAuth((state) => state.user);
  const [windowDays, setWindowDays] = useState<WindowValue>("7");
  const [trendDays, setTrendDays] = useState(30);
  const { t } = useI18n();

  const days = Number(windowDays);
  const overview = useOverview(days);
  const trend = useTimeseries(trendDays);
  const runs = useRuns({ page: 1, pageSize: 6 });

  const data = overview.data;
  const portfolio = data?.portfolio;

  const trendData = useMemo<TrendDatum[]>(
    () =>
      (trend.data ?? []).map((point) => ({
        label: point.date.slice(5),
        values: { cost: point.cost, revenue: point.revenue, roas: point.roas },
      })),
    [trend.data],
  );

  const alertSlices = useMemo(() => {
    const bySeverity = data?.alerts.by_severity ?? {};
    return [
      { id: "critical", label: t("Critical"), value: bySeverity.critical ?? 0, color: "var(--color-neg)" },
      { id: "warning", label: t("Warning"), value: bySeverity.warning ?? 0, color: "var(--color-warn)" },
      { id: "info", label: t("Info"), value: bySeverity.info ?? 0, color: "var(--color-accent-400)" },
    ].filter((slice) => slice.value > 0);
  }, [data, t]);

  const actionSlices = useMemo(() => {
    const byStatus = data?.actions.by_status ?? {};
    return [
      { id: "proposed", label: t("Awaiting approval"), value: byStatus.proposed ?? 0, color: "var(--color-brand-400)" },
      { id: "approved", label: t("Approved"), value: byStatus.approved ?? 0, color: "var(--color-accent-400)" },
      { id: "executed", label: t("Executed"), value: byStatus.executed ?? 0, color: "var(--color-pos)" },
      { id: "rejected", label: t("Rejected"), value: byStatus.rejected ?? 0, color: "var(--color-ink-3)" },
      { id: "failed", label: t("Failed"), value: byStatus.failed ?? 0, color: "var(--color-neg)" },
      { id: "skipped", label: t("Skipped"), value: byStatus.skipped ?? 0, color: "var(--color-warn)" },
    ].filter((slice) => slice.value > 0);
  }, [data, t]);

  const healthScore = data?.health.score ?? 0;
  const healthTone =
    healthScore >= 75 ? "positive" : healthScore >= 50 ? "warning" : "negative";

  const runColumns: Array<Column<Run>> = [
    {
      key: "id",
      header: t("Run"),
      cell: (row) => (
        <Link to={`/runs/${row.id}`} className="font-mono text-xs text-brand-300 hover:underline">
          {truncate(row.id, 18)}
        </Link>
      ),
    },
    { key: "status", header: t("Status"), cell: (row) => <StatusPill domain="run" value={row.status} /> },
    {
      key: "campaigns",
      header: t("Scope"),
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {row.campaign_ids.length === 0 ? t("all") : row.campaign_ids.length}
        </span>
      ),
    },
    {
      key: "actions",
      header: t("Proposals"),
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">
          {typeof row.summary === "object" && row.summary !== null && "actions" in row.summary
            ? formatNumber((row.summary as { actions?: number }).actions ?? 0)
            : "—"}
        </span>
      ),
    },
    {
      key: "cost",
      header: t("Model spend"),
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs text-ink-2">{formatCurrency(row.llm_cost_usd, { digits: 4 })}</span>
      ),
    },
    {
      key: "created",
      header: t("Started"),
      align: "right",
      cell: (row) => (
        <span className="text-xs text-ink-3">{formatRelative(row.created_at)}</span>
      ),
    },
  ];

  const loading = overview.isLoading;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("Portfolio overview")}
        description={t("Delivery, model output and outstanding operator work for the selected window.")}
        actions={
          <>
            <Tabs
              size="sm"
              items={WINDOWS.map((item) => ({ value: item.value, label: item.label }))}
              value={windowDays}
              onChange={setWindowDays}
            />
            {can(user, "run:trigger") && (
              <Button variant="primary" icon="play" onClick={() => navigate("/runs?new=1")}>
                {t("New run")}
              </Button>
            )}
          </>
        }
      />

      {overview.isError && (
        <ErrorNotice error={overview.error} onRetry={() => void overview.refetch()} />
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={t("Spend")}
          icon="wallet"
          tone="brand"
          loading={loading}
          value={formatCurrency(portfolio?.total_cost, { compact: true })}
          hint={t("{days}-day window", { days })}
        />
        <StatCard
          label={t("Revenue")}
          icon="trendUp"
          tone="positive"
          loading={loading}
          value={formatCurrency(portfolio?.total_revenue, { compact: true })}
          hint={t("{count} conversions", { count: formatCompact(portfolio?.conversions) })}
        />
        <StatCard
          label={t("ROAS")}
          icon="target"
          tone={(portfolio?.roas ?? 0) >= 2 ? "positive" : "warning"}
          loading={loading}
          value={formatRatio(portfolio?.roas)}
          hint={t("revenue ÷ spend")}
        />
        <StatCard
          label={t("CPA")}
          icon="users"
          tone="neutral"
          loading={loading}
          value={formatCurrency(portfolio?.cpa)}
          hint={t("cost per acquisition")}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={t("CTR")}
          icon="eye"
          loading={loading}
          value={formatPercent(portfolio?.ctr)}
          hint={t("{clicks} clicks / {impressions} impressions", { clicks: formatCompact(portfolio?.clicks), impressions: formatCompact(portfolio?.impressions) })}
        />
        <StatCard
          label={t("CVR")}
          icon="check"
          loading={loading}
          value={formatPercent(portfolio?.cvr)}
          hint={t("click → conversion")}
        />
        <StatCard
          label={t("Health score")}
          icon="activity"
          tone={healthTone}
          loading={loading}
          value={data ? data.health.score.toFixed(1) : "—"}
          unit="/ 100"
          hint={t(data?.health.status ?? "composite of ROAS, CPA and volume")}
        />
        <StatCard
          label={t("Needs approval")}
          icon="checkSquare"
          tone={(data?.actions.pending ?? 0) > 0 ? "warning" : "neutral"}
          loading={loading}
          value={formatNumber(data?.actions.pending)}
          hint={
            <Link to="/actions" className="text-brand-300 hover:underline">
              {t("open the queue →")}
            </Link>
          }
        />
      </div>

      <Card
        title={t("Delivery trend")}
        subtitle={t("Daily spend, revenue and ROAS across the last {days} days", { days: trendDays })}
        actions={
          <Tabs
            size="sm"
            items={[
              { value: "14", label: "14d" },
              { value: "30", label: "30d" },
              { value: "90", label: "90d" },
            ]}
            value={String(trendDays)}
            onChange={(value) => setTrendDays(Number(value))}
          />
        }
      >
        {trend.isLoading ? (
          <Skeleton className="h-60 w-full" />
        ) : (
          <TrendChart
            data={trendData}
            series={TREND_SERIES.map((series) => ({ ...series, label: t(series.label) }))}
            height={260}
            yLeftLabel="USD"
            yRightLabel="ROAS"
          />
        )}
      </Card>

      <div className="grid gap-3 lg:grid-cols-3">
        <Card title={t("Top campaigns by ROAS")} subtitle={t("Best performing in window")}>
          {loading ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            <BarList
              items={(data?.top_campaigns ?? []).map((campaign) => ({
                id: campaign.campaign_id,
                label: campaign.campaign_name || truncate(campaign.campaign_id, 22),
                value: campaign.roas,
                tone: campaign.roas >= 2 ? "positive" : "warning",
                hint: t("{spend} spend · {conv} conv", { spend: formatCurrency(campaign.cost, { compact: true }), conv: formatNumber(campaign.conversions) }),
                onClick: () => navigate(`/campaigns/${campaign.campaign_id}`),
              }))}
              format={(value) => `${value.toFixed(2)}x`}
              emptyLabel={t("No delivery in this window")}
            />
          )}
        </Card>

        <Card title={t("Needs attention")} subtitle={t("Worst ROAS in window")}>
          {loading ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            <BarList
              items={(data?.worst_campaigns ?? []).map((campaign) => ({
                id: campaign.campaign_id,
                label: campaign.campaign_name || truncate(campaign.campaign_id, 22),
                value: campaign.roas,
                tone: campaign.roas < 1 ? "negative" : "warning",
                hint: t("CTR {ctr} · CPA {cpa}", { ctr: formatPercent(campaign.ctr), cpa: formatCurrency(campaign.cpa) }),
                onClick: () => navigate(`/campaigns/${campaign.campaign_id}`),
              }))}
              format={(value) => `${value.toFixed(2)}x`}
              emptyLabel={t("No delivery in this window")}
            />
          )}
        </Card>

        <div className="flex flex-col gap-3">
          <Card
            title={t("Open alerts")}
            subtitle={t("{count} unresolved", { count: formatNumber(data?.alerts.open ?? 0) })}
            actions={
              <Link to="/alerts">
                <Button size="xs" variant="ghost" iconRight="chevronRight">
                  {t("View")}
                </Button>
              </Link>
            }
          >
            {loading ? (
              <Skeleton className="h-28 w-full" />
            ) : alertSlices.length === 0 ? (
              <div className="flex items-center gap-2.5 py-4">
                <span className="grid size-8 place-items-center rounded-full border border-pos/30 bg-pos/10 text-pos">
                  <Icon name="check" size={15} />
                </span>
                <div>
                  <p className="text-[13px] font-medium text-ink-1">{t("All clear")}</p>
                  <p className="text-xs text-ink-3">{t("No open anomalies in this window.")}</p>
                </div>
              </div>
            ) : (
              <Donut
                slices={alertSlices}
                size={132}
                thickness={15}
                centerLabel={t("open")}
                centerValue={formatNumber(data?.alerts.open ?? 0)}
              />
            )}
          </Card>

          <Card title={t("Campaign fleet")} subtitle={t("Lifecycle distribution")}>
            {loading ? (
              <Skeleton className="h-20 w-full" />
            ) : (
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-ink-3">{t("Total")}</span>
                  <span className="tnum text-sm font-semibold text-ink-1">
                    {formatNumber(data?.campaigns.total ?? 0)}
                  </span>
                </div>
                <div className="flex gap-2">
                  <Badge tone="positive" dot>
                    {formatNumber(data?.campaigns.active ?? 0)} {t("active")}
                  </Badge>
                  <Badge tone="warning" dot>
                    {formatNumber(data?.campaigns.paused ?? 0)} {t("paused")}
                  </Badge>
                </div>
                <Link to="/campaigns" className="mt-1 inline-flex items-center gap-1 text-xs text-brand-300 hover:underline">
                  {t("Manage campaigns")}
                  <Icon name="chevronRight" size={12} />
                </Link>
              </div>
            )}
          </Card>
        </div>
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <Card
          className="lg:col-span-2"
          title={t("Recent optimization runs")}
          subtitle={t("Latest supervisor executions")}
          padded={false}
          actions={
            <Link to="/runs">
              <Button size="xs" variant="ghost" iconRight="chevronRight">
                {t("All runs")}
              </Button>
            </Link>
          }
        >
          <Table<Run>
            columns={runColumns}
            rows={runs.data?.items ?? []}
            rowKey={(row) => row.id}
            loading={runs.isLoading}
            error={runs.isError ? t("Could not load recent runs") : undefined}
            onRowClick={(row) => navigate(`/runs/${row.id}`)}
            emptyTitle={t("No runs yet")}
            emptyHint={t("Trigger the supervisor to analyse delivery and propose changes.")}
            skeletonRows={4}
            dense
          />
        </Card>

        <Card title={t("Action pipeline")} subtitle={t("Where proposals stand")}>
          {loading ? (
            <Skeleton className="h-40 w-full" />
          ) : actionSlices.length === 0 ? (
            <p className="py-6 text-center text-xs text-ink-3">{t("No proposals recorded yet.")}</p>
          ) : (
            <Donut
              slices={actionSlices}
              size={150}
              thickness={17}
              centerLabel={t("proposals")}
              centerValue={formatNumber(
                actionSlices.reduce((sum, slice) => sum + slice.value, 0),
              )}
            />
          )}
        </Card>
      </div>
    </div>
  );
}