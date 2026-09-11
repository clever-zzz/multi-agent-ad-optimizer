import { useI18n } from "@/i18n";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { StatusPill } from "@/components/ui/StatusPill";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/cn";
import { formatCurrency, humanize } from "@/lib/format";
import { useCampaigns } from "@/hooks/useCampaigns";
import { useStartRun } from "@/hooks/useRuns";
import { toast } from "@/stores/toast";

export interface RunTriggerDialogProps {
  open: boolean;
  onClose: () => void;
  // Preselects campaigns when opened from a campaign context.
  initialCampaignIds?: string[];
}

export function RunTriggerDialog({ open, onClose, initialCampaignIds }: RunTriggerDialogProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const campaigns = useCampaigns({ page: 1, pageSize: 200 });
  // Destructured so the effect depends on stable function identities only.
  const { mutate: startRun, isPending, isError, error: runError, reset: resetRun } = useStartRun();

  const [selected, setSelected] = useState<string[]>([]);
  const [maxIterations, setMaxIterations] = useState(3);
  const [windowDays, setWindowDays] = useState(7);
  const [background, setBackground] = useState(true);
  const [search, setSearch] = useState("");

  const initialKey = (initialCampaignIds ?? []).join(",");

  useEffect(() => {
    if (!open) return;
    setSelected(initialKey ? initialKey.split(",") : []);
    resetRun();
  }, [open, initialKey, resetRun]);

  const rows = useMemo(() => {
    const items = campaigns.data?.items ?? [];
    const needle = search.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (campaign) =>
        campaign.name.toLowerCase().includes(needle) || campaign.platform.includes(needle),
    );
  }, [campaigns.data, search]);

  const toggle = (id: string) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  };

  const submit = () => {
    startRun(
      {
        campaign_ids: selected.length > 0 ? selected : null,
        max_iterations: maxIterations,
        window_days: windowDays,
        background,
      },
      {
        onSuccess: (run) => {
          toast.success(t("Run started"), t("Run {id} is {status}.", { id: run.id, status: humanize(run.status) }));
          onClose();
          void navigate(`/runs/${run.id}`);
        },
        onError: (error) => {
          toast.error(t("Could not start run"), (error as Error).message);
        },
      },
    );
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("Start an optimization run")}
      description={t("The supervisor loops monitor → audience → creative → bidding → optimize. It produces proposals; nothing is applied without approval.")}
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={isPending}>
            {t("Cancel")}
          </Button>
          <Button variant="primary" icon="play" onClick={submit} loading={isPending}>
            {background ? t("Start in background") : t("Start and wait")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {isError && <ErrorNotice error={runError} title={t("Run rejected")} />}

        <div className="grid gap-3 sm:grid-cols-3">
          <TextField
            label={t("Max iterations")}
            type="number"
            min={1}
            max={10}
            value={maxIterations}
            hint={t("1–10 supervisor loops")}
            onChange={(event) => setMaxIterations(Number(event.target.value))}
          />
          <TextField
            label={t("Lookback window (days)")}
            type="number"
            min={1}
            max={90}
            value={windowDays}
            hint={t("Telemetry fed to the agents")}
            onChange={(event) => setWindowDays(Number(event.target.value))}
          />
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-ink-2">{t("Execution")}</span>
            <div className="flex h-9.5 items-center gap-2 rounded-lg border border-line bg-surface-2 px-2.5">
              <input
                id="run-background"
                type="checkbox"
                checked={background}
                onChange={(event) => setBackground(event.target.checked)}
                className="size-3.5 accent-[var(--color-brand-500)]"
              />
              <label htmlFor="run-background" className="text-[13px] text-ink-1">
                {t("Background")}
              </label>
            </div>
            <p className="text-xs text-ink-3">{t("Stream progress live over SSE")}</p>
          </div>
        </div>

        <div>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-ink-2">{t("Scope")}</span>
              <span className="text-[11px] text-ink-3">
                {selected.length === 0 ? t("All active campaigns") : t("{count} selected", { count: selected.length })}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <Button size="xs" variant="ghost" onClick={() => setSelected(rows.map((row) => row.id))}>
                {t("Select visible")}
              </Button>
              <Button size="xs" variant="ghost" onClick={() => setSelected([])}>
                {t("Clear")}
              </Button>
            </div>
          </div>

          <div className="relative mb-2">
            <Icon
              name="search"
              size={14}
              className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-ink-3"
            />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={t("Filter campaigns…")}
              aria-label={t("Filter campaigns")}
              className="field pl-8"
            />
          </div>

          <div className="max-h-60 overflow-y-auto rounded-lg border border-line">
            {campaigns.isLoading ? (
              <p className="px-3 py-6 text-center text-xs text-ink-3">{t("Loading campaigns…")}</p>
            ) : rows.length === 0 ? (
              <p className="px-3 py-6 text-center text-xs text-ink-3">{t("No campaigns match.")}</p>
            ) : (
              <ul className="divide-y divide-line">
                {rows.map((campaign) => {
                  const checked = selected.includes(campaign.id);
                  return (
                    <li key={campaign.id}>
                      <label
                        className={cn(
                          "flex cursor-pointer items-center gap-3 px-3 py-2 transition-colors hover:bg-surface-2",
                          checked && "bg-brand-600/8",
                        )}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggle(campaign.id)}
                          className="size-3.5 shrink-0 accent-[var(--color-brand-500)]"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-[13px] text-ink-1">
                            {campaign.name}
                          </span>
                          <span className="tnum block truncate text-[11px] text-ink-3">
                            {humanize(campaign.platform)} · {t("daily {amount}", { amount: formatCurrency(campaign.daily_budget, { digits: 0 }) })}
                          </span>
                        </span>
                        <StatusPill domain="campaign" value={campaign.status} />
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}