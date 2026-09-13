import { useI18n } from "@/i18n";
import { useMemo, useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { ChangePasswordDialog } from "@/components/ChangePasswordDialog";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { SelectField, TextField } from "@/components/ui/Field";
import { Table, type Column } from "@/components/ui/Table";
import { StatusPill } from "@/components/ui/StatusPill";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import {
  useSystemInfo,
  useDependencyHealth,
  useSeed,
  usePrune,
  useUsers,
  useCreateUser,
  useUpdateUser,
} from "@/hooks/useAdmin";
import { useSpend } from "@/hooks/useAnalytics";
import { useAuth } from "@/stores/auth";
import { toast } from "@/stores/toast";
import {
  formatCurrency,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  humanize,
} from "@/lib/format";
import { passwordProblemText } from "@/lib/passwordPolicy";
import type { Role, User } from "@/lib/types";

const ROLE_OPTIONS: Array<{ value: Role; label: string }> = [
  { value: "admin", label: "Admin — full control, audit and user management" },
  { value: "optimizer", label: "Optimizer — can approve and execute proposals" },
  { value: "analyst", label: "Analyst — read plus alert acknowledgement" },
  { value: "viewer", label: "Viewer — read only" },
  { value: "ingestor", label: "Ingestor — metrics pipeline identity, can push data only" },
];

interface HealthEntry {
  status?: string;
  [key: string]: unknown;
}

function toneForStatus(status: string | undefined): "positive" | "warning" | "negative" | "neutral" {
  switch (status) {
    case "ok":
    case "ready":
    case "healthy":
      return "positive";
    case "degraded":
    case "disabled":
      return "warning";
    case "unavailable":
    case "error":
      return "negative";
    default:
      return "neutral";
  }
}

export function SettingsPage() {
  const { t } = useI18n();
  const user = useAuth((state) => state.user);
  const info = useSystemInfo();
  const health = useDependencyHealth({ refetchMs: 30_000 });
  const spend = useSpend(30);
  const seed = useSeed();
  const prune = usePrune();
  const users = useUsers();
  const createUser = useCreateUser();
  const updateUser = useUpdateUser();

  const [passwordOpen, setPasswordOpen] = useState(false);
  const [seedOpen, setSeedOpen] = useState(false);
  const [pruneOpen, setPruneOpen] = useState(false);
  const [pruneDays, setPruneDays] = useState("365");
  const [userModalOpen, setUserModalOpen] = useState(false);
  const [draft, setDraft] = useState({ email: "", password: "", full_name: "", role: "viewer" as Role });
  const [editTarget, setEditTarget] = useState<User | null>(null);
  const [editDraft, setEditDraft] = useState<{ role: Role; is_active: boolean }>({
    role: "viewer",
    is_active: true,
  });

  const config = info.data;
  const dependencies = (health.data ?? {}) as Record<string, HealthEntry>;

  // A production environment running on SQLite or with the approval gate off is a
  // real misconfiguration, so it is surfaced rather than buried in the config grid.
  const warnings = useMemo(() => {
    const found: Array<{ tone: "negative" | "warning"; text: string }> = [];
    if (!config) return found;
    if (config.environment === "production" && config.database_dialect === "sqlite") {
      found.push({
        tone: "negative",
        text: t("DATABASE__URL points at SQLite in production. Use PostgreSQL: SQLite cannot serve concurrent writers from multiple replicas."),
      });
    }
    if (config.environment === "production" && !config.require_action_approval) {
      found.push({
        tone: "negative",
        text: t("SECURITY__REQUIRE_ACTION_APPROVAL is off in production, so the optimizer can change budgets and bids without a human."),
      });
    }
    if (!config.redis_enabled) {
      found.push({
        tone: "warning",
        text: t("Redis is not connected; caching and rate limiting fall back to per-process memory, which is not shared across replicas."),
      });
    }
    if (!config.clickhouse_enabled) {
      found.push({
        tone: "warning",
        text: t("ClickHouse is not connected; telemetry aggregates are served from the relational database."),
      });
    }
    if (config.llm_provider === "mock") {
      found.push({
        tone: "warning",
        text: t("LLM__PROVIDER is mock. Creative generation returns deterministic templates instead of calling a model."),
      });
    }
    return found;
  }, [config, t]);

  const budgetUsed =
    config && spend.data?.month_to_date_usd !== undefined && spend.data.monthly_budget_usd
      ? spend.data.month_to_date_usd / spend.data.monthly_budget_usd
      : null;

  const openEdit = (row: User) => {
    setEditTarget(row);
    setEditDraft({ role: row.role, is_active: row.is_active });
  };

  const userColumns: Array<Column<User>> = [
    {
      key: "email",
      header: t("Account"),
      cell: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] text-ink-1">
            {row.full_name || row.email}
            {row.id === user?.id && <span className="ml-1.5 text-[11px] text-ink-3">{t("(you)")}</span>}
          </p>
          <p className="truncate text-[11px] text-ink-3">{row.email}</p>
        </div>
      ),
    },
    { key: "role", header: t("Role"), cell: (row) => <Badge tone={row.role === "admin" ? "brand" : "neutral"}>{humanize(row.role)}</Badge> },
    {
      key: "active",
      header: t("State"),
      cell: (row) =>
        row.is_active ? (
          <StatusPill domain="campaign" value="active" label={t("active")} />
        ) : (
          <StatusPill domain="campaign" value="paused" label={t("disabled")} />
        ),
    },
    {
      key: "must_change",
      header: t("Password"),
      cell: (row) =>
        row.must_change_password ? (
          <Badge tone="warning">{t("reset due")}</Badge>
        ) : (
          <span className="text-xs text-ink-3">{t("ok")}</span>
        ),
    },
    {
      key: "last_login",
      header: t("Last sign-in"),
      align: "right",
      cell: (row) => <span className="text-xs text-ink-3">{formatDateTime(row.last_login_at)}</span>,
    },
    {
      key: "created",
      header: t("Created"),
      align: "right",
      cell: (row) => <span className="text-xs text-ink-3">{formatDateTime(row.created_at)}</span>,
    },
    {
      key: "manage",
      header: "",
      align: "right",
      cell: (row) => (
        <Button variant="ghost" size="xs" onClick={() => openEdit(row)}>
          {t("Manage")}
        </Button>
      ),
    },
  ];

  const submitEdit = () => {
    if (!editTarget) return;
    const payload: { role?: Role; is_active?: boolean } = {};
    if (editDraft.role !== editTarget.role) payload.role = editDraft.role;
    if (editDraft.is_active !== editTarget.is_active) payload.is_active = editDraft.is_active;
    if (Object.keys(payload).length === 0) {
      setEditTarget(null);
      return;
    }
    updateUser.mutate(
      { userId: editTarget.id, payload },
      {
        onSuccess: (updated) => {
          toast.success(
            t("Account updated"),
            updated.is_active
              ? t("{email} is now {role}. Their sessions were revoked.", {
                  email: updated.email,
                  role: humanize(updated.role),
                })
              : t("{email} can no longer sign in.", { email: updated.email }),
          );
          setEditTarget(null);
        },
        onError: (error) => toast.error(t("Could not update account"), (error as Error).message),
      },
    );
  };

  const submitUser = () => {
    createUser.mutate(
      {
        email: draft.email.trim(),
        password: draft.password,
        role: draft.role,
        full_name: draft.full_name.trim(),
      },
      {
        onSuccess: (created) => {
          toast.success(
            t("Account created"),
            t("{email} must set a password at first sign-in.", { email: created.email }),
          );
          setUserModalOpen(false);
          setDraft({ email: "", password: "", full_name: "", role: "viewer" });
        },
        onError: (error) => toast.error(t("Could not create account"), (error as Error).message),
      },
    );
  };

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("System")}
        description={t("Runtime configuration, dependency health, model spend and account administration. Read from the live API, never from build-time constants.")}
        actions={
          <>
            <Button variant="secondary" icon="shield" onClick={() => setPasswordOpen(true)}>
              {t("Change my password")}
            </Button>
            <Button variant="ghost" icon="refresh" onClick={() => { void info.refetch(); void health.refetch(); }} loading={info.isFetching || health.isFetching}>
              {t("Refresh")}
            </Button>
          </>
        }
      />

      {info.isError && <ErrorNotice error={info.error} onRetry={() => void info.refetch()} title={t("Configuration unavailable")} />}

      {warnings.length > 0 && (
        <div className="flex flex-col gap-2">
          {warnings.map((warning) => (
            <div
              key={warning.text}
              className={
                warning.tone === "negative"
                  ? "flex items-start gap-2.5 rounded-lg border border-neg/35 bg-neg/8 px-3.5 py-2.5"
                  : "flex items-start gap-2.5 rounded-lg border border-warn/35 bg-warn/8 px-3.5 py-2.5"
              }
            >
              <Icon
                name="warning"
                size={15}
                className={warning.tone === "negative" ? "mt-0.5 shrink-0 text-neg" : "mt-0.5 shrink-0 text-warn"}
              />
              <p className="text-xs leading-relaxed text-ink-1">{warning.text}</p>
            </div>
          ))}
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title={t("Runtime configuration")} subtitle={t("Non-sensitive settings as the API sees them")}>
          {info.isLoading ? (
            <Skeleton className="h-56 w-full" />
          ) : config ? (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
              {[
                ["Environment", config.environment],
                ["Version", config.version],
                ["Data mode", config.data_mode],
                ["Database", config.database_dialect],
                ["LLM provider", config.llm_provider],
                ["LLM model", config.llm_model],
                ["Orchestrator", config.orchestrator_mode],
                ["Cache backend", config.cache_backend],
                ["ClickHouse", config.clickhouse_enabled ? t("enabled") : t("not connected")],
                ["Redis", config.redis_enabled ? t("enabled") : t("not connected")],
                ["Approval gate", config.require_action_approval ? t("enforced") : t("DISABLED")],
                ["Uptime", formatDuration(new Date(Date.now() - config.uptime_seconds * 1000).toISOString())],
              ].map(([label, value]) => (
                <div key={String(label)}>
                  <dt className="text-[10px] tracking-wide text-ink-3 uppercase">{t(String(label))}</dt>
                  <dd
                    className={
                      label === "Approval gate" && !config.require_action_approval
                        ? "mt-0.5 text-xs font-semibold text-neg"
                        : "mt-0.5 font-mono text-xs text-ink-1"
                    }
                  >
                    {String(value)}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <EmptyState icon="sliders" title={t("No configuration returned")} />
          )}
        </Card>

        <Card title={t("Dependency health")} subtitle={t("Probed live from the API container")} padded={false}>
          {health.isLoading ? (
            <div className="p-4">
              <Skeleton className="h-56 w-full" />
            </div>
          ) : Object.keys(dependencies).length === 0 ? (
            <EmptyState icon="database" title={t("No dependencies reported")} />
          ) : (
            <ul className="divide-y divide-line">
              {Object.entries(dependencies).map(([name, entry]) => {
                const status = String(entry.status ?? "unknown");
                const tone = toneForStatus(status);
                const detail = Object.entries(entry)
                  .filter(([key]) => key !== "status")
                  .map(([key, value]) => `${humanize(key)}: ${String(value)}`)
                  .join(" · ");
                return (
                  <li key={name} className="flex items-center gap-3 px-4 py-2.5">
                    <span
                      className={
                        tone === "positive"
                          ? "size-2 shrink-0 rounded-full bg-pos"
                          : tone === "warning"
                            ? "size-2 shrink-0 rounded-full bg-warn"
                            : tone === "negative"
                              ? "size-2 shrink-0 rounded-full bg-neg"
                              : "size-2 shrink-0 rounded-full bg-ink-3"
                      }
                    />
                    <div className="min-w-0 flex-1">
                      <p className="text-[13px] font-medium text-ink-1">{humanize(name)}</p>
                      {detail && <p className="truncate text-[11px] text-ink-3">{detail}</p>}
                    </div>
                    <Badge tone={tone}>{humanize(status)}</Badge>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title={t("Model spend")}
        subtitle={t("Recorded by the LLM gateway ledger, including retries and fallback calls")}
        actions={
          spend.data?.monthly_budget_usd ? (
            <Badge tone={budgetUsed !== null && budgetUsed > 0.8 ? "warning" : "neutral"}>
              {budgetUsed !== null
                ? t("{pct} of monthly guardrail", { pct: formatPercent(budgetUsed, 1) })
                : t("guardrail set")}
            </Badge>
          ) : undefined
        }
      >
        {spend.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <div className="flex flex-col gap-4">
            <div className="grid gap-3 sm:grid-cols-4">
              {[
                ["Window", t("{count} days", { count: spend.data?.window_days ?? 30 })],
                ["Total cost", formatCurrency(spend.data?.total_cost_usd ?? 0, { digits: 4 })],
                ["Total calls", formatNumber(spend.data?.total_calls ?? 0)],
                ["Month to date", formatCurrency(spend.data?.month_to_date_usd ?? 0, { digits: 4 })],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
                  <p className="text-[10px] tracking-wide text-ink-3 uppercase">{t(String(label))}</p>
                  <p className="tnum mt-0.5 text-base font-semibold text-ink-1">{value}</p>
                </div>
              ))}
            </div>

            {budgetUsed !== null && spend.data?.monthly_budget_usd ? (
              <div>
                <div className="mb-1 flex items-baseline justify-between text-xs">
                  <span className="text-ink-3">
                    {t("Monthly guardrail {amount}", {
                      amount: formatCurrency(spend.data.monthly_budget_usd, { digits: 0 }),
                    })}
                  </span>
                  <span className="tnum text-ink-2">
                    {t("{amount} used", {
                      amount: formatCurrency(spend.data.month_to_date_usd ?? 0, { digits: 4 }),
                    })}
                  </span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-surface-3">
                  <div
                    className={
                      budgetUsed > 0.9
                        ? "h-full rounded-full bg-neg"
                        : budgetUsed > 0.7
                          ? "h-full rounded-full bg-warn"
                          : "h-full rounded-full bg-pos"
                    }
                    style={{ width: `${Math.min(100, budgetUsed * 100)}%` }}
                  />
                </div>
                <p className="mt-1.5 text-[11px] text-ink-3">
                  {t("The gateway refuses new calls once the guardrail is exhausted and falls back to deterministic templates, so a runaway loop cannot produce an unbounded bill.")}
                </p>
              </div>
            ) : null}

            {(spend.data?.by_model ?? []).length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-[13px]">
                  <thead>
                    <tr className="border-b border-line">
                      {[t("Provider"), t("Model"), t("Calls"), t("Prompt tokens"), t("Completion tokens"), t("Cost")].map((header, index) => (
                        <th
                          key={header}
                          className={
                            index < 2
                              ? "px-2 py-1.5 text-left text-[11px] font-semibold tracking-wider text-ink-3 uppercase"
                              : "px-2 py-1.5 text-right text-[11px] font-semibold tracking-wider text-ink-3 uppercase"
                          }
                        >
                          {header}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(spend.data?.by_model ?? []).map((row) => (
                      <tr key={`${row.provider}-${row.model}`} className="border-b border-line/60 last:border-b-0">
                        <td className="px-2 py-1.5 text-ink-2">{row.provider}</td>
                        <td className="px-2 py-1.5 font-mono text-xs text-ink-1">{row.model}</td>
                        <td className="tnum px-2 py-1.5 text-right text-ink-2">{formatNumber(row.calls)}</td>
                        <td className="tnum px-2 py-1.5 text-right text-ink-2">{formatNumber(row.prompt_tokens)}</td>
                        <td className="tnum px-2 py-1.5 text-right text-ink-2">{formatNumber(row.completion_tokens)}</td>
                        <td className="tnum px-2 py-1.5 text-right font-medium text-ink-1">
                          {formatCurrency(row.cost_usd, { digits: 4 })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-xs text-ink-3">{t("No model calls recorded in this window.")}</p>
            )}
          </div>
        )}
      </Card>

      <Card title={t("Accounts")} subtitle={t("Roles map to permissions server-side; the console only reflects them")} padded={false}
        actions={
          <Button variant="primary" size="xs" icon="plus" onClick={() => setUserModalOpen(true)}>
            {t("New account")}
          </Button>
        }
      >
        <Table<User>
          columns={userColumns}
          rows={users.data ?? []}
          rowKey={(row) => row.id}
          loading={users.isLoading}
          emptyTitle={t("No accounts")}
          skeletonRows={3}
          dense
        />
      </Card>

      <Card title={t("Data management")} subtitle={t("Demonstration dataset and retention policy")}>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-lg border border-line bg-surface-2 p-3.5">
            <p className="text-[13px] font-semibold text-ink-1">{t("Seed demo dataset")}</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-3">
              {t("Creates campaigns, creatives and 90 days of deterministic daily metrics so every view has data. Idempotent: running it again skips unless forced.")}
            </p>
            <Button className="mt-3" variant="secondary" icon="database" onClick={() => setSeedOpen(true)}>
              {t("Load dataset")}
            </Button>
          </div>

          <div className="rounded-lg border border-line bg-surface-2 p-3.5">
            <p className="text-[13px] font-semibold text-ink-1">{t("Apply retention policy")}</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-3">
              {t("Deletes audit rows older than the retention window and clears expired idempotency keys. Audit retention is bounded to at least 30 days server-side.")}
            </p>
            <div className="mt-3 flex items-end gap-2">
              <TextField
                wrapClassName="w-32"
                label={t("Keep audit for (days)")}
                type="number"
                min={30}
                max={3650}
                value={pruneDays}
                onChange={(event) => setPruneDays(event.target.value)}
              />
              <Button variant="secondary" icon="trash" onClick={() => setPruneOpen(true)}>
                {t("Prune now")}
              </Button>
            </div>
          </div>
        </div>
      </Card>

      <ChangePasswordDialog open={passwordOpen} onClose={() => setPasswordOpen(false)} />

      <ConfirmDialog
        open={seedOpen}
        title={t("Load the demo dataset")}
        tone="primary"
        busy={seed.isPending}
        confirmLabel="Seed database"
        message={t("Adds campaigns, creatives and 90 days of daily metrics. Existing rows are left untouched unless the dataset is already present, in which case nothing happens.")}
        onConfirm={() =>
          seed.mutate(false, {
            onSuccess: (result) => {
              toast.success(
                result.skipped ? t("Dataset already present") : t("Demo dataset loaded"),
                t("{campaigns} campaigns · {creatives} creatives · {rows} metric rows", {
                  campaigns: result.campaigns,
                  creatives: result.creatives,
                  rows: result.daily_rows,
                }),
              );
              setSeedOpen(false);
            },
            onError: (error) => toast.error(t("Seeding failed"), (error as Error).message),
          })
        }
        onCancel={() => setSeedOpen(false)}
      />

      <ConfirmDialog
        open={pruneOpen}
        title={t("Apply retention policy")}
        tone="danger"
        busy={prune.isPending}
        confirmLabel="Delete expired rows"
        message={
          <>
            <p>
              {t("Audit entries older than {days} and all expired idempotency keys will be deleted.", {
                days: pruneDays,
              })}
            </p>
            <p className="mt-2 text-xs text-ink-3">
              {t("Check your jurisdictional retention obligations before shortening this window.")}
            </p>
          </>
        }
        onConfirm={() =>
          prune.mutate(Number(pruneDays), {
            onSuccess: (result) => {
              toast.success(
                t("Retention applied"),
                t("{audit} audit rows and {keys} idempotency keys removed", {
                  audit: formatNumber(result.audit_logs_removed),
                  keys: formatNumber(result.idempotency_keys_removed),
                }),
              );
              setPruneOpen(false);
            },
            onError: (error) => toast.error(t("Prune failed"), (error as Error).message),
          })
        }
        onCancel={() => setPruneOpen(false)}
      />

      <Modal
        open={userModalOpen}
        onClose={() => setUserModalOpen(false)}
        title={t("Create an account")}
        description={t("New accounts are flagged must-change-password and cannot sign in with the provisional password for long.")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setUserModalOpen(false)} disabled={createUser.isPending}>
              {t("Cancel")}
            </Button>
            <Button
              variant="primary"
              icon="plus"
              onClick={submitUser}
              loading={createUser.isPending}
              disabled={!draft.email || passwordProblemText(draft.password) !== undefined}
            >
              {t("Create account")}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {createUser.isError && <ErrorNotice error={createUser.error} title={t("Account not created")} />}
          <TextField
            label={t("Email")}
            type="email"
            autoComplete="off"
            value={draft.email}
            onChange={(event) => setDraft((prev) => ({ ...prev, email: event.target.value }))}
            required
          />
          <TextField
            label={t("Full name")}
            autoComplete="off"
            value={draft.full_name}
            onChange={(event) => setDraft((prev) => ({ ...prev, full_name: event.target.value }))}
          />
          <TextField
            label={t("Provisional password")}
            type="password"
            autoComplete="new-password"
            value={draft.password}
            onChange={(event) => setDraft((prev) => ({ ...prev, password: event.target.value }))}
            error={passwordProblemText(draft.password)}
            hint={t("Shared once over a secure channel; the operator must replace it at first sign-in.")}
            required
          />
          <SelectField
            label={t("Role")}
            value={draft.role}
            options={ROLE_OPTIONS}
            onChange={(event) => setDraft((prev) => ({ ...prev, role: event.target.value as Role }))}
            hint={t("Permissions are resolved from the role on every request.")}
          />
        </div>
      </Modal>

      <Modal
        open={editTarget !== null}
        onClose={() => setEditTarget(null)}
        title={t("Manage account")}
        description={
          editTarget
            ? t("{email}. Changing a role or disabling the account revokes every one of its sessions immediately.", {
                email: editTarget.email,
              })
            : undefined
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditTarget(null)} disabled={updateUser.isPending}>
              {t("Cancel")}
            </Button>
            <Button variant="primary" icon="shield" onClick={submitEdit} loading={updateUser.isPending}>
              {t("Save changes")}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {updateUser.isError && <ErrorNotice error={updateUser.error} title={t("Account not updated")} />}
          <SelectField
            label={t("Role")}
            value={editDraft.role}
            options={ROLE_OPTIONS}
            onChange={(event) => setEditDraft((prev) => ({ ...prev, role: event.target.value as Role }))}
            hint={t("The last active administrator cannot be demoted.")}
          />
          <label className="flex items-start gap-2.5 rounded-lg border border-line bg-surface-2 p-3">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-brand"
              checked={editDraft.is_active}
              onChange={(event) => setEditDraft((prev) => ({ ...prev, is_active: event.target.checked }))}
            />
            <span>
              <span className="block text-[13px] font-medium text-ink-1">{t("Account enabled")}</span>
              <span className="mt-0.5 block text-xs leading-relaxed text-ink-3">
                {t("Disabling blocks sign-in and ends all live sessions. The last active administrator cannot be disabled.")}
              </span>
            </span>
          </label>
        </div>
      </Modal>
    </div>
  );
}