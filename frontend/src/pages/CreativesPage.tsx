import { useI18n } from "@/i18n";
import { useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/StatCard";
import { Donut } from "@/components/charts/Donut";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { SelectField } from "@/components/ui/Field";
import { Table, type Column } from "@/components/ui/Table";
import { Pagination } from "@/components/ui/Pagination";
import { StatusPill } from "@/components/ui/StatusPill";
import { Modal } from "@/components/ui/Modal";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { useCreativeLibrary, useCreativeSummary } from "@/hooks/useCreatives";
import { useCampaignNames } from "@/hooks/useCampaigns";
import { useDebounced } from "@/hooks/useDebounced";
import { usePagination } from "@/hooks/usePagination";
import { formatNumber, formatPercent, formatRelative, humanize, truncate } from "@/lib/format";
import type { Creative, CreativeStatus } from "@/lib/types";

const STATUS_OPTIONS: Array<{ value: CreativeStatus; label: string }> = [
  { value: "draft", label: "Draft" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
  { value: "rejected", label: "Rejected" },
];

const ORIGIN_OPTIONS = [
  { value: "llm", label: "Model generated" },
  { value: "rule", label: "Rule template" },
  { value: "human", label: "Human authored" },
];

const TYPE_OPTIONS = [
  { value: "text", label: "Text" },
  { value: "image", label: "Image" },
  { value: "video", label: "Video" },
];

// Origin values come from the API contract, so the display key is resolved here
    // once and reused by both the filter options and the detail modal.
function originLabelKey(origin: string): string {
  if (origin === "llm") return "Model generated";
  if (origin === "rule") return "Rule template";
  return "Human authored";
}

const SORT_OPTIONS = [
  { value: "created_at", label: "Newest first" },
  { value: "score", label: "Highest score" },
  { value: "headline", label: "Headline A–Z" },
  { value: "status", label: "Status" },
];

export function CreativesPage() {
  const { t } = useI18n();
  const pagination = usePagination(25);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<CreativeStatus | "">("");
  const [origin, setOrigin] = useState<"human" | "llm" | "rule" | "">("");
  const [type, setType] = useState<"text" | "image" | "video" | "">("");
  const [sort, setSort] = useState<"created_at" | "score" | "headline" | "status">("created_at");
  const [detail, setDetail] = useState<Creative | null>(null);

  const debouncedSearch = useDebounced(search, 300);
  const campaignNames = useCampaignNames();
  const library = useCreativeLibrary({
    page: pagination.page,
    pageSize: pagination.pageSize,
    status,
    origin,
    type,
    search: debouncedSearch,
    sort,
    order: sort === "headline" ? "asc" : "desc",
  });
  const summary = useCreativeSummary();

  const rows = library.data?.items ?? [];
  const totals = summary.data;

  const originSlices = Object.entries(totals?.by_origin ?? {}).map(([key, value]) => ({
    id: key,
    label: t(originLabelKey(key)),
    value,
    color:
      key === "llm"
        ? "var(--color-violet)"
        : key === "rule"
          ? "var(--color-accent-400)"
          : "var(--color-brand-400)",
  }));

  const columns: Array<Column<Creative>> = [
    {
      key: "headline",
      header: t("Creative"),
      cell: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink-1">{row.headline}</p>
          {row.description && <p className="truncate text-[11px] text-ink-3">{truncate(row.description, 96)}</p>}
        </div>
      ),
      className: "max-w-lg",
    },
    {
      key: "campaign",
      header: t("Campaign"),
      cell: (row) => (
        <Link to={`/campaigns/${row.campaign_id}`} className="text-xs text-ink-2 hover:text-brand-300 hover:underline">
          {campaignNames.get(row.campaign_id) ?? truncate(row.campaign_id, 18)}
        </Link>
      ),
      className: "max-w-44",
    },
    { key: "type", header: t("Type"), cell: (row) => <Badge>{row.creative_type}</Badge> },
    {
      key: "origin",
      header: t("Origin"),
      cell: (row) => (
        <Badge tone={row.origin === "llm" ? "violet" : row.origin === "rule" ? "info" : "neutral"}>
          {row.origin === "llm" ? "model" : row.origin}
        </Badge>
      ),
    },
    {
      key: "emotion",
      header: t("Emotion"),
      cell: (row) => <span className="text-xs text-ink-2">{row.target_emotion || "—"}</span>,
    },
    { key: "group", header: t("Test group"), cell: (row) => <span className="text-xs text-ink-2">{row.ab_group}</span> },
    { key: "status", header: t("Status"), cell: (row) => <StatusPill domain="creative" value={row.status} /> },
    {
      key: "score",
      header: t("Score"),
      align: "right",
      cell: (row) =>
        row.score === null ? (
          <span className="text-xs text-ink-3">—</span>
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
    {
      key: "open",
      header: "",
      align: "right",
      cell: (row) => (
        <Button size="xs" variant="ghost" icon="eye" onClick={() => setDetail(row)} aria-label={t("Inspect {name}", { name: row.headline })} />
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("Creative library")}
        description={t("Every headline the account can serve, with provenance. Model-generated copy is labelled as such and scored on live delivery rather than on how good it reads.")}
      />

      <div className="grid gap-3 lg:grid-cols-[repeat(3,minmax(0,1fr))_minmax(0,1.2fr)]">
        <StatCard
          label={t("Total creatives")}
          icon="sparkles"
          loading={summary.isLoading}
          value={formatNumber(totals?.total)}
          hint={t("{count} scored on delivery", { count: formatNumber(totals?.scored) })}
        />
        <StatCard
          label={t("Model authored")}
          icon="cpu"
          tone="violet"
          loading={summary.isLoading}
          value={formatNumber(totals?.generated)}
          hint={t("{share} of the library", { share: formatPercent(totals?.generated_share, 1) })}
        />
        <StatCard
          label={t("Awaiting review")}
          icon="eye"
          tone={(totals?.by_status.draft ?? 0) > 0 ? "warning" : "neutral"}
          loading={summary.isLoading}
          value={formatNumber(totals?.by_status.draft)}
          hint={t("draft status")}
        />
        <Card title={t("Provenance")} subtitle={t("Where each creative came from")}>
          {originSlices.length === 0 ? (
            <p className="py-6 text-center text-xs text-ink-3">{t("No creatives recorded yet.")}</p>
          ) : (
            <Donut
              slices={originSlices}
              size={126}
              thickness={15}
              centerLabel={t("creatives")}
              centerValue={formatNumber(totals?.total ?? 0)}
            />
          )}
        </Card>
      </div>

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
              placeholder={t("Search headline or body copy…")}
              aria-label={t("Search creatives")}
              className="field pl-8"
            />
          </div>
          <SelectField
            wrapClassName="w-36"
            label={t("Status")}
            value={status}
            placeholder={t("All")}
            options={STATUS_OPTIONS}
            onChange={(event) => {
              setStatus(event.target.value as CreativeStatus | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-44"
            label={t("Origin")}
            value={origin}
            placeholder={t("All origins")}
            options={ORIGIN_OPTIONS}
            onChange={(event) => {
              setOrigin(event.target.value as "human" | "llm" | "rule" | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-32"
            label={t("Type")}
            value={type}
            placeholder={t("All")}
            options={TYPE_OPTIONS}
            onChange={(event) => {
              setType(event.target.value as "text" | "image" | "video" | "");
              pagination.reset();
            }}
          />
          <SelectField
            wrapClassName="w-44"
            label={t("Sort")}
            value={sort}
            options={SORT_OPTIONS}
            onChange={(event) => {
              setSort(event.target.value as typeof sort);
              pagination.reset();
            }}
          />
        </div>

        {library.isError && (
          <div className="px-4 pt-3">
            <ErrorNotice error={library.error} onRetry={() => void library.refetch()} />
          </div>
        )}

        <Table<Creative>
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          loading={library.isFetching}
          onRowClick={(row) => setDetail(row)}
          emptyTitle={t("No creatives match")}
          emptyHint={t("Clear a filter, or run the optimizer — the creative agent writes variants for underperforming campaigns.")}
          skeletonRows={8}
        />

        <Pagination
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={library.data?.total ?? 0}
          onChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />
      </Card>

      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail?.headline ?? ""}
        description={
          detail
            ? t("{type} creative · {origin}", {
                type: humanize(detail.creative_type),
                origin: t(originLabelKey(detail.origin)),
              })
            : undefined
        }
        size="md"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDetail(null)}>
              {t("Close")}
            </Button>
            {detail && (
              <Link to={`/campaigns/${detail.campaign_id}`}>
                <Button variant="primary" iconRight="chevronRight">
                  {t("Open campaign")}
                </Button>
              </Link>
            )}
          </>
        }
      >
        {detail && (
          <div className="flex flex-col gap-3.5">
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill domain="creative" value={detail.status} />
              <Badge tone={detail.origin === "llm" ? "violet" : "neutral"}>{detail.origin}</Badge>
              <Badge>{detail.creative_type}</Badge>
              <Badge tone="info">{detail.ab_group}</Badge>
              {detail.target_emotion && <Badge tone="warning">{detail.target_emotion}</Badge>}
            </div>

            <div className="rounded-lg border border-line bg-surface-2 p-4">
              <p className="text-[10px] tracking-wide text-ink-3 uppercase">{t("Headline")}</p>
              <p className="mt-1 text-base font-semibold text-ink-1">{detail.headline}</p>
              {detail.description && (
                <>
                  <p className="mt-3 text-[10px] tracking-wide text-ink-3 uppercase">{t("Body")}</p>
                  <p className="mt-1 text-[13px] leading-relaxed text-ink-2">{detail.description}</p>
                </>
              )}
              <p className="mt-3 text-[10px] tracking-wide text-ink-3 uppercase">{t("Call to action")}</p>
              <p className="mt-1 inline-flex rounded-md bg-brand-600 px-3 py-1.5 text-[13px] font-semibold text-white">
                {detail.cta_text || "—"}
              </p>
            </div>

            <dl className="grid grid-cols-2 gap-3">
              {[
                [t("Campaign"), campaignNames.get(detail.campaign_id) ?? detail.campaign_id],
                [t("Performance score"), detail.score === null ? t("unscored") : detail.score.toFixed(2)],
                [t("Generated by run"), detail.generated_by_run_id ?? "—"],
                [t("Created"), formatRelative(detail.created_at)],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-[10px] tracking-wide text-ink-3 uppercase">{label}</dt>
                  <dd className="mt-0.5 truncate text-xs text-ink-1" title={String(value)}>
                    {String(value)}
                  </dd>
                </div>
              ))}
            </dl>

            {detail.generated_by_run_id && (
              <Link to={`/runs/${detail.generated_by_run_id}`} className="inline-flex items-center gap-1.5 text-xs text-brand-300 hover:underline">
                <Icon name="external" size={12} />
                {t("Inspect the run that produced this creative")}
              </Link>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}