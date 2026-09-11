import { useI18n } from "@/i18n";
import { useEffect, useMemo, useState } from "react";

import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { SelectField, TextField, TextAreaField } from "@/components/ui/Field";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { describeError } from "@/lib/errors";
import { ApiError } from "@/lib/api";
import { useCreateCampaign, useUpdateCampaign } from "@/hooks/useCampaigns";
import { toast } from "@/stores/toast";
import type { Campaign, CampaignStatus, Platform } from "@/lib/types";

const PLATFORMS: Array<{ value: Platform; label: string }> = [
  { value: "google", label: "Google Ads" },
  { value: "meta", label: "Meta Ads" },
  { value: "tiktok", label: "TikTok Ads" },
  { value: "mock", label: "Mock adapter (local)" },
];

const STATUSES: Array<{ value: CampaignStatus; label: string }> = [
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
  { value: "completed", label: "Completed" },
  { value: "archived", label: "Archived" },
];

const OBJECTIVES = ["conversions", "traffic", "awareness", "app_installs", "lead_generation"];

interface FormState {
  name: string;
  platform: Platform;
  status: CampaignStatus;
  daily_budget: string;
  total_budget: string;
  target_cpa: string;
  target_roas: string;
  objective: string;
  start_date: string;
  end_date: string;
  target_audience: string;
  external_id: string;
}

function toForm(campaign: Campaign | null): FormState {
  if (!campaign) {
    return {
      name: "",
      platform: "mock",
      status: "active",
      daily_budget: "500",
      total_budget: "0",
      target_cpa: "45",
      target_roas: "2.5",
      objective: "conversions",
      start_date: new Date().toISOString().slice(0, 10),
      end_date: "",
      target_audience: "",
      external_id: "",
    };
  }
  return {
    name: campaign.name,
    platform: campaign.platform,
    status: campaign.status,
    daily_budget: String(campaign.daily_budget),
    total_budget: String(campaign.total_budget),
    target_cpa: String(campaign.target_cpa),
    target_roas: String(campaign.target_roas),
    objective: campaign.objective || "conversions",
    start_date: campaign.start_date,
    end_date: campaign.end_date ?? "",
    target_audience: campaign.target_audience,
    external_id: campaign.external_id ?? "",
  };
}

export interface CampaignFormDialogProps {
  open: boolean;
  onClose: () => void;
  campaign?: Campaign | null;
}

export function CampaignFormDialog({ open, onClose, campaign }: CampaignFormDialogProps) {
  const { t } = useI18n();
  const editing = Boolean(campaign);
  const createCampaign = useCreateCampaign();
  const updateCampaign = useUpdateCampaign(campaign?.id ?? "");

  const [form, setForm] = useState<FormState>(() => toForm(campaign ?? null));
  const [formError, setFormError] = useState<unknown>(null);

  useEffect(() => {
    if (!open) return;
    setForm(toForm(campaign ?? null));
    setFormError(null);
    createCampaign.reset();
    updateCampaign.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, campaign?.id]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const numericErrors = useMemo(() => {
    const problems: Partial<Record<keyof FormState, string>> = {};
    const daily = Number(form.daily_budget);
    const total = Number(form.total_budget || 0);
    const cpa = Number(form.target_cpa);
    const roas = Number(form.target_roas);

    if (!Number.isFinite(daily) || daily <= 0) problems.daily_budget = t("Must be greater than 0");
    if (!Number.isFinite(total) || total < 0) problems.total_budget = t("Cannot be negative");
    if (total > 0 && daily > 0 && total < daily) {
      problems.total_budget = t("Total budget cannot be below the daily budget");
    }
    if (!Number.isFinite(cpa) || cpa <= 0) problems.target_cpa = t("Must be greater than 0");
    if (!Number.isFinite(roas) || roas <= 0) problems.target_roas = t("Must be greater than 0");
    if (form.name.trim().length < 2) problems.name = t("At least 2 characters");
    if (form.end_date && form.start_date && form.end_date < form.start_date) {
      problems.end_date = t("Cannot precede the start date");
    }
    return problems;
  }, [form, t]);

  const busy = createCampaign.isPending || updateCampaign.isPending;
  const hasErrors = Object.keys(numericErrors).length > 0;

  const submit = () => {
    setFormError(null);
    const payload = {
      name: form.name.trim(),
      platform: form.platform,
      daily_budget: Number(form.daily_budget),
      total_budget: Number(form.total_budget || 0),
      target_cpa: Number(form.target_cpa),
      target_roas: Number(form.target_roas),
      objective: form.objective,
      start_date: form.start_date || null,
      end_date: form.end_date || null,
      target_audience: form.target_audience.trim(),
      external_id: form.external_id.trim() || null,
    };

    const onError = (error: unknown) => {
      setFormError(error);
      toast.error(
        editing ? t("Could not save campaign") : t("Could not create campaign"),
        describeError(error),
      );
    };

    if (editing && campaign) {
      updateCampaign.mutate(
        { ...payload, status: form.status },
        {
          onSuccess: (saved) => {
            toast.success(t("Campaign updated"), saved.name);
            onClose();
          },
          onError,
        },
      );
      return;
    }

    createCampaign.mutate(payload, {
      onSuccess: (saved) => {
        toast.success(t("Campaign created"), t("{name} is {status}.", { name: saved.name, status: saved.status }));
        onClose();
      },
      onError,
    });
  };

  const apiError = createCampaign.error ?? updateCampaign.error ?? formError;
  const fieldError = (key: keyof FormState): string | undefined => {
    if (numericErrors[key]) return numericErrors[key];
    if (apiError instanceof ApiError) return apiError.fieldMessage(key);
    return undefined;
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={editing ? t("Edit campaign") : t("New campaign")}
      description={t("Targets drive every agent decision: the bidding agent anchors on target CPA and ROAS.")}
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t("Cancel")}
          </Button>
          <Button
            variant="primary"
            icon={editing ? "check" : "plus"}
            onClick={submit}
            disabled={hasErrors}
            loading={busy}
          >
            {editing ? t("Save changes") : t("Create campaign")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {apiError ? (
          <ErrorNotice error={apiError} title={editing ? t("Save rejected") : t("Creation rejected")} />
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2">
          <TextField
            label={t("Campaign name")}
            value={form.name}
            onChange={(event) => set("name", event.target.value)}
            error={fieldError("name")}
            placeholder={t("Q3 retargeting — EU")}
            wrapClassName="sm:col-span-2"
            required
          />
          <SelectField
            label={t("Platform")}
            value={form.platform}
            options={PLATFORMS.map((item) => ({ value: item.value, label: t(item.label) }))}
            onChange={(event) => set("platform", event.target.value as Platform)}
            disabled={editing}
            hint={editing ? t("Platform cannot change after creation") : t("Determines the adapter used for execution")}
          />
          {editing && (
            <SelectField
              label={t("Status")}
              value={form.status}
              options={STATUSES.map((item) => ({ value: item.value, label: t(item.label) }))}
              onChange={(event) => set("status", event.target.value as CampaignStatus)}
            />
          )}
          {!editing && (
            <SelectField
              label={t("Objective")}
              value={form.objective}
              options={OBJECTIVES.map((value) => ({ value, label: t(value.replace(/_/g, " ")) }))}
              onChange={(event) => set("objective", event.target.value)}
            />
          )}
          {editing && (
            <TextField
              label={t("External ID")}
              value={form.external_id}
              onChange={(event) => set("external_id", event.target.value)}
              error={fieldError("external_id")}
              hint={t("Identifier on the ad platform")}
            />
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <TextField
            label={t("Daily budget (USD)")}
            type="number"
            min={1}
            step="10"
            value={form.daily_budget}
            onChange={(event) => set("daily_budget", event.target.value)}
            error={fieldError("daily_budget")}
            required
          />
          <TextField
            label={t("Total budget (USD)")}
            type="number"
            min={0}
            step="100"
            value={form.total_budget}
            onChange={(event) => set("total_budget", event.target.value)}
            error={fieldError("total_budget")}
            hint={t("0 means uncapped")}
          />
          <TextField
            label={t("Target CPA (USD)")}
            type="number"
            min={0.01}
            step="1"
            value={form.target_cpa}
            onChange={(event) => set("target_cpa", event.target.value)}
            error={fieldError("target_cpa")}
            required
          />
          <TextField
            label={t("Target ROAS")}
            type="number"
            min={0.01}
            step="0.1"
            value={form.target_roas}
            onChange={(event) => set("target_roas", event.target.value)}
            error={fieldError("target_roas")}
            required
          />
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <TextField
            label={t("Start date")}
            type="date"
            value={form.start_date}
            onChange={(event) => set("start_date", event.target.value)}
          />
          <TextField
            label={t("End date")}
            type="date"
            value={form.end_date}
            onChange={(event) => set("end_date", event.target.value)}
            error={fieldError("end_date")}
            hint={t("Leave empty for always-on")}
          />
          {!editing && (
            <TextField
              label={t("External ID")}
              value={form.external_id}
              onChange={(event) => set("external_id", event.target.value)}
              error={fieldError("external_id")}
              hint={t("Optional platform identifier")}
            />
          )}
        </div>

        <TextAreaField
          label={t("Target audience")}
          rows={3}
          value={form.target_audience}
          onChange={(event) => set("target_audience", event.target.value)}
          placeholder={t("25–44, urban, previous purchasers, interest in running gear")}
          hint={t("Free text consumed by the audience agent to build segments and lookalikes")}
        />
      </div>
    </Modal>
  );
}