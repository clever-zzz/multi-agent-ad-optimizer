import { useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { SelectField } from "@/components/ui/Field";
import { Table, type Column } from "@/components/ui/Table";
import { Pagination } from "@/components/ui/Pagination";
import { StatusPill } from "@/components/ui/StatusPill";
import { Modal } from "@/components/ui/Modal";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { EmptyState } from "@/components/ui/EmptyState";
import { useABTests } from "@/hooks/useAdmin";
import { useCampaignNames } from "@/hooks/useCampaigns";
import { usePagination } from "@/hooks/usePagination";
import { formatDateTime, formatNumber, formatPercent, humanize, truncate } from "@/lib/format";
import type { ABTest } from "@/lib/types";

export function ExperimentsPage() {
  const pagination = usePagination(25);
  const [campaignId, setCampaignId] = useState("");
  const [detail, setDetail] = useState<ABTest | null>(null);

  const campaignNames = useCampaignNames();
  const experiments = useABTests({
    page: pagination.page,
    pageSize: pagination.pageSize,
    campaignId: campaignId || undefined,
  });

  const rows = experiments.data?.items ?? [];

  const campaignOptions = [...campaignNames.entries()].map(([id, name]) => ({
    value: id,
    label: truncate(name, 34),
  }));

  const columns: Array<Column<ABTest>> = [
    {
      key: "name",
      header: "Experiment",
      cell: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink-1">{row.name}</p>
          {row.hypothesis && <p className="truncate text-[11px] text-ink-3">{truncate(row.hypothesis, 90)}</p>}
        </div>
      ),
      className: "max-w-md",
    },
    {
      key: "campaign",
      header: "Campaign",
      cell: (row) => (
        <Link to={`/campaigns/${row.campaign_id}`} className="text-xs text-ink-2 hover:text-brand-300 hover:underline">
          {campaignNames.get(row.campaign_id) ?? truncate(row.campaign_id, 18)}
        </Link>
      ),
      className: "max-w-44",
    },
    { key: "status", header: "Status", cell: (row) => <StatusPill domain="abtest" value={row.status} /> },
    { key: "metric", header: "Metric", cell: (row) => <Badge>{row.metric.toUpperCase()}</Badge> },
    {
      key: "mde",
      header: "MDE",
      align: "right",
      cell: (row) => <span className="tnum text-xs text-ink-2">{formatPercent(row.minimum_detectable_effect, 1)}</span>,
    },
    {
      key: "sample",
      header: "Required sample",
      align: "right",
      cell: (row) => <span className="tnum text-xs text-ink-2">{formatNumber(row.required_sample_size)}</span>,
    },
    {
      key: "split",
      header: "Split",
      align: "right",
      cell: (row) => <span className="tnum text-xs text-ink-2">{formatPercent(row.traffic_split, 0)}</span>,
    },
    {
      key: "winner",
      header: "Outcome",
      cell: (row) =>
        row.winner_creative_id ? (
          <Badge tone="positive">winner declared</Badge>
        ) : row.status === "concluded" ? (
          <Badge tone="neutral">inconclusive</Badge>
        ) : (
          <span className="text-xs text-ink-3">—</span>
        ),
    },
    {
      key: "started",
      header: "Started",
      align: "right",
      cell: (row) => <span className="text-xs text-ink-3">{row.started_at ? formatDateTime(row.started_at) : "—"}</span>,
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (row) => <Button size="xs" variant="ghost" icon="eye" onClick={() => setDetail(row)} aria-label={`Inspect ${row.name}`} />,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Experiments"
        description="A/B tests created by the optimizer. Each one carries an explicit hypothesis, a minimum detectable effect and the sample size required before a winner may be declared."
      />

      <div className="grid gap-3 lg:grid-cols-3">
        <Card className="lg:col-span-3" title="Why experiments are pre-registered" padded>
          <p className="max-w-4xl text-[13px] leading-relaxed text-ink-2">
            Declaring a winner before the required sample size is reached is the most common way
            ad-tech dashboards invent lift. Every test here stores its metric, minimum detectable
            effect and required sample at creation time, so the conclusion can be checked against
            what was promised rather than chosen after the fact.
          </p>
        </Card>
      </div>

      <Card padded={false}>
        <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
          <SelectField
            wrapClassName="w-64"
            label="Campaign"
            value={campaignId}
            placeholder="All campaigns"
            options={campaignOptions}
            onChange={(event) => {
              setCampaignId(event.target.value);
              pagination.reset();
            }}
          />
          <Button className="ml-auto" variant="ghost" icon="refresh" onClick={() => void experiments.refetch()} loading={experiments.isFetching}>
            Refresh
          </Button>
        </div>

        {experiments.isError && (
          <div className="px-4 pt-3">
            <ErrorNotice error={experiments.error} onRetry={() => void experiments.refetch()} />
          </div>
        )}

        <Table<ABTest>
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          loading={experiments.isFetching}
          onRowClick={(row) => setDetail(row)}
          emptyTitle="No experiments yet"
          emptyHint="The optimizer proposes start_ab_test actions when two creatives are statistically indistinguishable but look different."
          emptyAction={
            <Link to="/actions">
              <Button variant="secondary" icon="checkSquare">
                Open approval queue
              </Button>
            </Link>
          }
          skeletonRows={5}
        />

        <Pagination
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={experiments.data?.total ?? 0}
          onChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />
      </Card>

      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail?.name ?? ""}
        description={detail ? `${humanize(detail.status)} · ${detail.metric.toUpperCase()} test` : undefined}
        size="lg"
        footer={
          <Button variant="ghost" onClick={() => setDetail(null)}>
            Close
          </Button>
        }
      >
        {detail && (
          <div className="flex flex-col gap-3.5">
            {detail.hypothesis && (
              <div className="rounded-lg border border-line bg-surface-2 p-3.5">
                <p className="text-[10px] tracking-wide text-ink-3 uppercase">Hypothesis</p>
                <p className="mt-1 text-[13px] leading-relaxed text-ink-1">{detail.hypothesis}</p>
              </div>
            )}

            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {[
                ["Status", humanize(detail.status)],
                ["Primary metric", detail.metric.toUpperCase()],
                ["Traffic split", formatPercent(detail.traffic_split, 0)],
                ["Minimum detectable effect", formatPercent(detail.minimum_detectable_effect, 1)],
                ["Required sample size", formatNumber(detail.required_sample_size)],
                ["Campaign", campaignNames.get(detail.campaign_id) ?? detail.campaign_id],
                ["Started", detail.started_at ? formatDateTime(detail.started_at) : "not started"],
                ["Concluded", detail.concluded_at ? formatDateTime(detail.concluded_at) : "—"],
                ["Winner", detail.winner_creative_id ? truncate(detail.winner_creative_id, 18) : "none declared"],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-[10px] tracking-wide text-ink-3 uppercase">{label}</dt>
                  <dd className="mt-0.5 truncate text-xs text-ink-1" title={String(value)}>
                    {String(value)}
                  </dd>
                </div>
              ))}
            </dl>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-line p-3">
                <p className="text-[10px] tracking-wide text-ink-3 uppercase">Control</p>
                <p className="mt-1 truncate font-mono text-xs text-ink-1">
                  {detail.control_creative_id ?? "—"}
                </p>
              </div>
              <div className="rounded-lg border border-violet/30 bg-violet/5 p-3">
                <p className="text-[10px] tracking-wide text-violet uppercase">Variant</p>
                <p className="mt-1 truncate font-mono text-xs text-ink-1">
                  {detail.variant_creative_id ?? "—"}
                </p>
              </div>
            </div>

            <div>
              <p className="mb-1.5 text-[10px] tracking-wide text-ink-3 uppercase">Result</p>
              {Object.keys(detail.result).length === 0 ? (
                <EmptyState
                  className="py-6"
                  icon="flask"
                  title="No result recorded"
                  hint="The test has not concluded, or it ended without enough evidence to declare a winner."
                />
              ) : (
                <pre className="max-h-64 overflow-auto rounded-lg border border-line bg-surface-2 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
                  {JSON.stringify(detail.result, null, 2)}
                </pre>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}