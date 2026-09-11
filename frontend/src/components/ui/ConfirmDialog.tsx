import { useI18n } from "@/i18n";
import type { ReactNode } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "danger" | "primary" | "success";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "danger",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const { t } = useI18n();
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {t(cancelLabel)}
          </Button>
          <Button
            variant={tone === "danger" ? "danger" : tone === "success" ? "success" : "primary"}
            onClick={onConfirm}
            loading={busy}
          >
            {t(confirmLabel)}
          </Button>
        </>
      }
    >
      <div className="text-[13px] leading-relaxed text-ink-2">{message}</div>
    </Modal>
  );
}