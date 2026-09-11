import { useI18n } from "@/i18n";
import { useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { TextField } from "@/components/ui/Field";
import { Table, type Column } from "@/components/ui/Table";
import { Pagination } from "@/components/ui/Pagination";
import { Modal } from "@/components/ui/Modal";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { useAuditTrail } from "@/hooks/useAdmin";
import { usePagination } from "@/hooks/usePagination";
import { formatDateTime, humanize, truncate } from "@/lib/format";
import type { AuditEntry } from "@/lib/types";

// Actions that change money or permissions get flagged so a reviewer can scan
// for the entries that actually matter.
const SENSITIVE = new Set([
  "action.approved",
  "action.executed",
  "action.rejected",
  "campaign.updated",
  "campaign.deleted",
  "user.created",
  "user.role_changed",
  "user.deactivated",
  "password.changed",
  "budget.adjusted",
]);

export function AuditPage() {
  const { t } = useI18n();
  const pagination = usePagination(50);
  const [action, setAction] = useState("");
  const [resourceType, setResourceType] = useState("");
  const [detail, setDetail] = useState<AuditEntry | null>(null);

  const audit = useAuditTrail({
    page: pagination.page,
    pageSize: pagination.pageSize,
    action: action || undefined,
    resourceType: resourceType || undefined,
  });

  const rows = audit.data?.items ?? [];

  const columns: Array<Column<AuditEntry>> = [
    {
      key: "created",
      header: t("Timestamp"),
      cell: (row) => <span className="tnum text-xs text-ink-2">{formatDateTime(row.created_at)}</span>,
    },
    {
      key: "actor",
      header: t("Actor"),
      cell: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] text-ink-1">{row.actor_email || t("system")}</p>
          <p className="text-[11px] text-ink-3">{t(row.actor_role || "—")}</p>
        </div>
      ),
      className: "max-w-52",
    },
    {
      key: "action",
      header: t("Action"),
      cell: (row) => (
        <span className="flex items-center gap-1.5">
          {SENSITIVE.has(row.action) && <Icon name="warning" size={12} className="shrink-0 text-warn" />}
          <span className="font-mono text-xs text-ink-1">{row.action}</span>
        </span>
      ),
    },
    {
      key: "resource",
      header: t("Resource"),
      cell: (row) => (
        <span className="text-xs text-ink-2">
          <Badge>{humanize(row.resource_type)}</Badge>{" "}
          <span className="font-mono text-ink-3">{truncate(row.resource_id ?? "—", 18)}</span>
        </span>
      ),
    },
    {
      key: "ip",
      header: t("Source IP"),
      cell: (row) => <span className="font-mono text-xs text-ink-3">{row.ip_address || "—"}</span>,
    },
    {
      key: "request",
      header: t("Request ID"),
      cell: (row) => <span className="font-mono text-[11px] text-ink-3">{truncate(row.request_id, 14)}</span>,
    },
    {
      key: "diff",
      header: "",
      align: "right",
      cell: (row) =>
        row.before || row.after ? (
          <Button size="xs" variant="ghost" icon="eye" onClick={() => setDetail(row)} aria-label={t("Inspect change")}>
            {t("Diff")}
          </Button>
        ) : (
          <span className="text-xs text-ink-3">—</span>
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("Audit log")}
        description={t("Append-only record of every privileged operation, with the before and after state of the resource it touched. Correlate any entry to a request via its request ID.")}
      />

      <Card padded={false}>
        <div className="flex flex-wrap items-end gap-3 border-b border-line px-4 py-3">
          <TextField
            wrapClassName="w-56"
            label={t("Action")}
            value={action}
            placeholder={t("e.g. action.approved")}
            onChange={(event) => {
              setAction(event.target.value);
              pagination.reset();
            }}
          />
          <TextField
            wrapClassName="w-48"
            label={t("Resource type")}
            value={resourceType}
            placeholder={t("e.g. campaign")}
            onChange={(event) => {
              setResourceType(event.target.value);
              pagination.reset();
            }}
          />
          {(action || resourceType) && (
            <Button
              variant="ghost"
              icon="close"
              onClick={() => {
                setAction("");
                setResourceType("");
                pagination.reset();
              }}
            >
              {t("Clear")}
            </Button>
          )}
          <Button className="ml-auto" variant="ghost" icon="refresh" onClick={() => void audit.refetch()} loading={audit.isFetching}>
            {t("Refresh")}
          </Button>
        </div>

        {audit.isError && (
          <div className="px-4 pt-3">
            <ErrorNotice error={audit.error} onRetry={() => void audit.refetch()} />
          </div>
        )}

        <Table<AuditEntry>
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          loading={audit.isFetching}
          onRowClick={(row) => (row.before || row.after ? setDetail(row) : undefined)}
          emptyTitle={t("No audit entries")}
          emptyHint={t("Entries appear as soon as an operator or the optimizer changes state.")}
          skeletonRows={10}
          dense
        />

        <Pagination
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={audit.data?.total ?? 0}
          onChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
          pageSizeOptions={[25, 50, 100, 200]}
        />
      </Card>

      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail ? detail.action : ""}
        description={detail ? t("{actor} · {time}", { actor: detail.actor_email || t("system"), time: formatDateTime(detail.created_at) }) : undefined}
        size="lg"
        footer={
          <Button variant="ghost" onClick={() => setDetail(null)}>
            {t("Close")}
          </Button>
        }
      >
        {detail && (
          <div className="flex flex-col gap-3.5">
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                [t("Actor role"), t(detail.actor_role || "—")],
                [t("Resource"), humanize(detail.resource_type)],
                [t("Resource ID"), detail.resource_id ?? "—"],
                [t("Source IP"), detail.ip_address || "—"],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-[10px] tracking-wide text-ink-3 uppercase">{label}</dt>
                  <dd className="mt-0.5 truncate font-mono text-xs text-ink-1" title={String(value)}>
                    {String(value)}
                  </dd>
                </div>
              ))}
            </dl>

            <div className="grid gap-3 lg:grid-cols-2">
              <div>
                <p className="mb-1.5 text-[10px] tracking-wide text-neg uppercase">{t("Before")}</p>
                <pre className="max-h-72 overflow-auto rounded-lg border border-neg/25 bg-neg/5 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
                  {detail.before ? JSON.stringify(detail.before, null, 2) : "null"}
                </pre>
              </div>
              <div>
                <p className="mb-1.5 text-[10px] tracking-wide text-pos uppercase">{t("After")}</p>
                <pre className="max-h-72 overflow-auto rounded-lg border border-pos/25 bg-pos/5 p-3 font-mono text-[11px] leading-relaxed text-ink-2">
                  {detail.after ? JSON.stringify(detail.after, null, 2) : "null"}
                </pre>
              </div>
            </div>

            <p className="tnum flex items-center gap-1.5 font-mono text-[11px] text-ink-3">
              <Icon name="info" size={12} />
              request_id: {detail.request_id || "—"}
            </p>
          </div>
        )}
      </Modal>
    </div>
  );
}