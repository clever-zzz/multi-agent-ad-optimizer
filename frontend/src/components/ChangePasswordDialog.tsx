import { useEffect, useState } from "react";

import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { ErrorNotice } from "@/components/ui/ErrorNotice";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/cn";
import { passwordProblems, PASSWORD_MIN_LENGTH } from "@/lib/passwordPolicy";
import { useAuth } from "@/stores/auth";
import { toast } from "@/stores/toast";

export interface ChangePasswordDialogProps {
  open: boolean;
  onClose: () => void;
  mandatory?: boolean;
}

export function ChangePasswordDialog({
  open,
  onClose,
  mandatory = false,
}: ChangePasswordDialogProps) {
  const changePassword = useAuth((state) => state.changePassword);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setCurrent("");
    setNext("");
    setConfirm("");
    setError(null);
    setBusy(false);
  }, [open]);

  const problems = passwordProblems(next);
  const mismatch = confirm.length > 0 && next !== confirm;
  const reused = next.length > 0 && next === current;
  const valid = current.length > 0 && problems.length === 0 && next === confirm && !reused;

  const checks = [
    { label: `${PASSWORD_MIN_LENGTH}+ characters`, ok: next.length >= PASSWORD_MIN_LENGTH },
    { label: "letter", ok: /[A-Za-z]/.test(next) },
    { label: "digit", ok: /[0-9]/.test(next) },
    { label: "symbol", ok: problems.length === 0 || !problems.includes("a symbol") },
    { label: "matches confirmation", ok: next.length > 0 && next === confirm },
  ];

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await changePassword(current, next);
      toast.success("Password updated", "Use the new password at your next sign-in.");
      onClose();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      dismissible={!mandatory}
      title={mandatory ? "Password change required" : "Change password"}
      description={
        mandatory
          ? "This account was created with the bootstrap password. Set a personal one before continuing."
          : "Choose a strong password that you do not reuse on any other service."
      }
      closeOnBackdrop={!mandatory}
      footer={
        <>
          {!mandatory && (
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
          )}
          <Button
            variant="primary"
            icon="shield"
            onClick={() => void submit()}
            disabled={!valid}
            loading={busy}
          >
            Update password
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {error ? <ErrorNotice error={error} title="Password not accepted" /> : null}
        {reused && (
          <p className="flex items-center gap-1.5 text-xs text-warn">
            <Icon name="warning" size={13} />
            New password must differ from the current one.
          </p>
        )}

        <TextField
          label="Current password"
          type="password"
          autoComplete="current-password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
          required
        />
        <TextField
          label="New password"
          type="password"
          autoComplete="new-password"
          value={next}
          onChange={(event) => setNext(event.target.value)}
          error={problems.length > 0 ? `Password needs ${problems.join(", ")}` : undefined}
          required
        />
        <TextField
          label="Confirm new password"
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          error={mismatch ? "Passwords do not match" : undefined}
          required
        />

        <ul className="flex flex-wrap gap-x-3 gap-y-1 rounded-lg border border-line bg-surface-2 px-3 py-2">
          {checks.map((check) => (
            <li
              key={check.label}
              className={cn(
                "inline-flex items-center gap-1 text-[11px]",
                check.ok ? "text-pos" : "text-ink-3",
              )}
            >
              <Icon name={check.ok ? "check" : "close"} size={11} />
              {check.label}
            </li>
          ))}
        </ul>
      </div>
    </Modal>
  );
}