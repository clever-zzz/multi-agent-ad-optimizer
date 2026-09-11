import { useI18n, localizeOptions } from "@/i18n";
import { useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/StatCard";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { SelectField } from "@/components/ui/Field";
import { Table, type Column } from "@/components/ui/Table";
import { Pagination } from "@/components/ui/Pagination";
import { StatusPill } from "@/components/ui/StatusPill";
import { Modal } from "@/components/ui/Modal";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { useAlerts, useAlertSummary, useAcknowledgeAlert, useResolveAlert } from "@/hooks/useAlerts";
import { useDetectAnomalies } from "@/hooks/useAnalytics";
import { useCampaignNames } from "@/hooks/useCampaigns";
import { usePagination } from "@/hooks/usePagination";
import { useAuth, can } from "@/stores/auth";
import { toast } from "@/stores/toast";
import { formatDateTime, formatNumber, humanize, truncate } from "@/lib/format";
import type { Alert, AlertRule, AlertSeverity, AlertStatus } from "@/lib/types";

const STATUS_OPTIONS: Array<{ value: AlertStatus; label: string }> = [
  { value: "open", label: "Open" },
  { value: "acknowledged", label: "Acknowledged" },
  { value: "resolved", label: "Resolved" },
];

const SEVERITY_OPTIONS: Array<{ value: AlertSeverity; label: string }> = [
  { value: "critical", label: "Critical" },
  { value: "warning", label: "Warning" },
  { value: "info", label: "Info" },
];

const RULE_OPTIONS: Array<{ value: AlertRule; label: string }> = [
  { value: "low_ctr", label: "Low CTR" },
  { value: "high_cpa", label: "High CPA" },
  { value: "low_roas", label: "Low ROAS" },
  { value: "burn_rate", label: "Burn rate" },
  { value: "impression_collapse", label: "Impression collapse" },
  { value: "frequency_fatigue", label: "Frequency fatigue" },
];

// What each rule actually measures, so an operator can judge the alert without
// reading the detection code.
const RULE_EXPLAINERS: Record<AlertRule, string> = {
  low_ctr: "Click-through rate is below the configured floor for the window.",
  high_cpa: "Cost per acquisition exceeds the campaign target by the configured tolerance.",
  low_roas: "Return on ad spend is below the break-even or target threshold.",
  burn_rate: "Spend is running far ahead of the daily budget pace.",
  impression_collapse: "Impressions dropped sharply against the trailing baseline.",
  frequency_fatigue: "Average frequency per unique reach is high enough to expect creative fatigue.",
};

export function AlertsPage() {
  const { t } = useI18n();
  const user = useAuth((state) => state.user);
  const canAck = can(user, "alert:ack");

  const pagination = usePagination(25);
  const [status, setStatus] = useState<AlertStatus | "">("open");
  const [severity, setSeverity] = useState<AlertSeverity | "">("");
  const [rule, setRule] = useState<AlertRule | "">("");
  const [detail, setDetail] = useState<Alert | null>(null);

  const campaignNames = useCampaignNames();
  const alerts = useAlerts({
    page: pagination.page,
    pageSize: pagination.pageSize,
    status,
    severity,
    rule,
  });
  const summary = useAlertSummary();
  const acknowledge = useAcknowledgeAlert();
  const resolve = useResolveAlert();
  const detect = useDetectAnomalies();

  const rows = alerts.data?.items ?? [];
  const counts = summary.data;
  const bySeverity = counts?.by_severity ?? {};

  const act = (mutation: typeof acknowledge, alert: Alert, verb: string) => {
    if (!alert.id) return;
    mutation.mutate(
      { alertId: alert.id },
      {
        onSuccess: () =>
          toast.success(
            verb === "acknowledged" ? t("Alert acknowledged") : t("Alert resolved"),
            humanize(alert.rule),
          ),
        onError: (error) =>
          toast.error(
            verb === "acknowledged" ? t("Could not acknowledge alert") : t("Could not resolve alert"),
            (error as Error).message,
          ),
      },
    );
  };

  const columns: Array<Column<Alert>> = [
    {
      key: "severity",
      header: t("Severity"),
      cell: (row) => <StatusPill domain="severity" value={row.severity} />,
    },
    {
      key: "rule",
      header: t("Rule"),
      cell: (row) => (
        <div className="min-w-0">
          <p className="text-[13px] font-medium text-ink-1">{humanize(row.rule)}</p>
          <p className="truncate text-[11px] text-ink-3">{t(RULE_EXPLAINERS[row.rule])}</p>
        </div>
      ),
      className: "max-w-72",
    },
    {
      key: "campaign",
      header: t("Campaign"),
      cell: (row) => (
        <Link to={`/campaigns/${row.campaign_id}`} className="text-xs text-ink-2 hover:text-brand-300 hover:underline">
          {campaignNames.get(row.campaign_id) ?? truncate(row.campaign_id, 20)}
        </Link>
      ),
      className: "max-w-48",
    },
    {
      key: "message",
      header: t("Detail"),
      cell: (row) => <span className="text-xs text-ink-2">{truncate(row.message, 90)}</span>,
      className: "max-w-sm",
    },
    {
      key: "observed",
      header: t("Observed"),
      align: "right",
      cell: (row) => (
        <span className="tnum text-xs">
          <span className="font-semibold text-neg">
            {row.observed === null ? "—" : row.observed.toFixed(3)}
          </span>
          <span className="text-ink-3"> / {row.threshold.toFixed(3)}</span>
        </span>
      ),
    },
    { key: "status", header: t("Status"), cell: (row) => <StatusPill domain="alert" value={row.status} /> },
    {
      key: "detected",
      header: t("Detected"),
      align: "right",
      cell: (row) => <span className="text-xs text-ink-3">{formatDateTime(row.detected_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (row) => (
        <div className="flex items-center justify-end gap-1">
          {canAck && row.status === "open" && (
            <Button
              size="xs"
              variant="secondary"
              onClick={() => act(acknowledge, row, "acknowledged")}
              disabled={acknowledge.isPending}
            >
              {t("Acknowledge")}
            </Button>
          )}
          {canAck && row.status !== "resolved" && (
            <Button
              size="xs"
              variant="ghost"
              icon="check"
              onClick={() => act(resolve, row, "resolved")}
              disabled={resolve.isPending}
            >
              {t("Resolve")}
            </Button>
          )}
          <Button size="xs" variant="ghost" icon="eye" onClick={() => setDetail(row)} aria-label={t("Inspect alert")}>
            {t("Context")}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("Alerts")}
        description={t("Raised by the monitor agent from statistical rules, not from model opinion. Each alert records the observed value against the threshold that fired.")}
        actions={
          <Button
            variant="secondary"
            icon="activity"
            loading={detect.isPending}
            onClick={() =>
              detect.mutate(
                { days: 7 },
                {
                  onSuccess: (found) =>
                    toast.info(
                      t("Detection complete"),
                      found.length === 0
                        ? t("No thresholds breached in the last 7 days.")
                        : t("{count} alert(s) raised.", { count: found.length }),
                    ),
                  onError: (error) => toast.error(t("Detection failed"), (error as Error).message),
                },
              )
            }
          >
            {t("Run detection now")}
          </Button>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <StatCard
          label={t("Open")}
          icon="bell"
          tone={(counts?.open ?? 0) > 0 ? "warning" : "positive"}
          loading={summary.isLoading}
          value={formatNumber(counts?.open)}
          hint={t("not yet resolved")}
        />
        <StatCard
          label={t("Critical")}
          icon="warning"
          tone={(bySeverity.critical ?? 0) > 0 ? "negative" : "neutral"}
          loading={summary.isLoading}
          value={formatNumber(bySeverity.critical)}
          hint={t("immediate attention")}
        />
        <StatCard
          label={t("Warning")}
          icon="warning"
          tone={(bySeverity.warning ?? 0) > 0 ? "warning" : "neutral"}
          loading={summary.isLoading}
          value={formatNumber(bySeverity.warning)}
        />
        <StatCard
          label={t("Acknowledged")}
          icon="check"
          loading={summary.isLoading}
          value={formatNumber(counts?.acknowledged)}
          hint={t("seen by an operator")}
        />
        <StatCard
          label={t("Resolved")}
          icon="checkSquare"
          tone="positive"
          loading={summary.isLoading}
          value={formatNumber(counts?.resolved)}
          hint={t("closed")}
        />
      </div>

      <Card padded={false}>
        <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
          <SelectField
            wrapClassName="w-40"
            label={t("Status")}
            value={status}
            placeholder={t("All")}
            options={localizeOptions(t, STATUS_OPTIONS)}
            onChange={(event) => {
              setStatus(event.target.value as AlertStatus | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-36"
            label={t("Severity")}
            value={severity}
            placeholder={t("All")}
            options={localizeOptions(t, SEVERITY_OPTIONS)}
            onChange={(event) => {
              setSeverity(event.target.value as AlertSeverity | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-52"
            label={t("Rule")}
            value={rule}
            placeholder={t("All rules")}
            options={localizeOptions(t, RULE_OPTIONS)}
            onChange={(event) => {
              setRule(event.target.value as AlertRule | "");
              pagination.reset();
            }}
          />
          {(status || severity || rule) && (
            <Button
              variant="ghost"
              icon="close"
              onClick={() => {
                setStatus("");
                setSeverity("");
                setRule("");
                pagination.reset();
              }}
            >
              {t("Clear filters")}
            </Button>
          )}
          <Button className="ml-auto" variant="ghost" icon="refresh" onClick={() => void alerts.refetch()} loading={alerts.isFetching}>
            {t("Refresh")}
          </Button>
        </div>

        {alerts.isError && (
          <div className="px-4 pt-3">
            <ErrorNotice error={alerts.error} onRetry={() => void alerts.refetch()} />
          </div>
        )}

        <Table<Alert>
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id ?? row.dedup_key}
          loading={alerts.isFetching}
          onRowClick={(row) => setDetail(row)}
          emptyTitle={t("No alerts match")}
          emptyHint={t("Delivery is inside every configured threshold for these filters.")}
          skeletonRows={6}
        />

        <Pagination
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={alerts.data?.total ?? 0}
          onChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />
      </Card>

      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail ? humanize(detail.rule) : ""}
        description={detail ? t(RULE_EXPLAINERS[detail.rule]) : undefined}
        size="md"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDetail(null)}>
              {t("Close")}
            </Button>
            {canAck && detail?.status === "open" && (
              <Button variant="primary" icon="check" onClick={() => detail && act(acknowledge, detail, "acknowledged")}>
                {t("Acknowledge")}
              </Button>
            )}
          </>
        }
      >
        {detail && (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill domain="severity" value={detail.severity} />
              <StatusPill domain="alert" value={detail.status} />
              {detail.run_id && (
                <Link to={`/runs/${detail.run_id}`}>
                  <Button size="xs" variant="ghost" iconRight="external">
                    {t("Source run")}
                  </Button>
                </Link>
              )}
            </div>

            <p className="text-[13px] leading-relaxed text-ink-1">{detail.message}</p>

            <dl className="grid grid-cols-2 gap-3 rounded-lg border border-line bg-surface-2 p-3">
              {[
                [t("Campaign"), campaignNames.get(detail.campaign_id) ?? detail.campaign_id],
                [t("Detected"), formatDateTime(detail.detected_at)],
                [t("Observed"), detail.observed === null ? "—" : detail.observed.toFixed(4)],
                [t("Threshold"), detail.threshold.toFixed(4)],
                [t("Acknowledged by"), detail.acknowledged_by ?? "—"],
                [t("Dedup key"), detail.dedup_key],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-[10px] tracking-wide text-ink-3 uppercase">{label}</dt>
                  <dd className="mt-0.5 truncate font-mono text-xs text-ink-1" title={String(value)}>
                    {String(value)}
                  </dd>
                </div>
              ))}
            </dl>

            <div>
              <p className="mb-1.5 text-[11px] tracking-wide text-ink-3 uppercase">{t("Detection context")}</p>
              <pre className="max-h-56 overflow-auto rounded-lg border border-line bg-surface-2 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
                {JSON.stringify(detail.context, null, 2)}
              </pre>
            </div>

            <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-ink-3">
              <Icon name="info" size={12} className="mt-0.5 shrink-0" />
              {t(
                "Alerts are de-duplicated by key, so the same condition on the same campaign does not raise a new row every run.",
              )}
            </p>
          </div>
        )}
      </Modal>
    </div>
  );
}