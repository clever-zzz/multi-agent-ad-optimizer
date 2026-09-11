import { useI18n } from "@/i18n";
import { ApiError } from "@/lib/api";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/cn";
import { describeError } from "@/lib/errors";

export interface ErrorNoticeProps {
  error: unknown;
  onRetry?: () => void;
  className?: string;
  title?: string;
}

export function ErrorNotice({ error, onRetry, className, title = "Request failed" }: ErrorNoticeProps) {
  const { t } = useI18n();
  if (!error) return null;
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2.5 rounded-lg border border-neg/35 bg-neg/8 px-3.5 py-3",
        className,
      )}
    >
      <Icon name="warning" size={16} className="mt-0.5 shrink-0 text-neg" />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-ink-1">{t(title)}</p>
        <p className="mt-0.5 text-xs break-words text-ink-2">{t(describeError(error))}</p>
        {error instanceof ApiError && error.requestId && (
          <p className="tnum mt-1 font-mono text-[11px] text-ink-3">request_id: {error.requestId}</p>
        )}
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded-md border border-line-strong px-2 py-1 text-xs text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink-1"
        >
          {t("Retry")}
        </button>
      )}
    </div>
  );
}