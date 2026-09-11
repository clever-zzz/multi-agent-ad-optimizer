import { useI18n } from "@/i18n";
import { cn } from "@/lib/cn";
import { useToasts, type ToastKind } from "@/stores/toast";
import { Icon, type IconName } from "@/components/ui/Icon";

const KIND_STYLE: Record<ToastKind, { className: string; icon: IconName }> = {
  success: { className: "border-pos/40 bg-pos/10 text-pos", icon: "check" },
  error: { className: "border-neg/40 bg-neg/10 text-neg", icon: "warning" },
  warning: { className: "border-warn/40 bg-warn/10 text-warn", icon: "warning" },
  info: { className: "border-brand-500/40 bg-brand-500/10 text-brand-300", icon: "info" },
};

export function Toaster() {
  const { t } = useI18n();
  const toasts = useToasts((state) => state.toasts);
  const dismiss = useToasts((state) => state.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div
      className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {toasts.map((item) => {
        const style = KIND_STYLE[item.kind];
        return (
          <div
            key={item.id}
            className={cn(
              "card pointer-events-auto flex items-start gap-2.5 border px-3.5 py-3 shadow-xl shadow-black/40",
              style.className,
            )}
          >
            <Icon name={style.icon} size={16} className="mt-0.5 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-semibold text-ink-1">{item.title}</p>
              {item.message && (
                <p className="mt-0.5 text-xs break-words text-ink-2">{item.message}</p>
              )}
            </div>
            <button
              type="button"
              onClick={() => dismiss(item.id)}
              aria-label={t("Dismiss notification")}
              className="-mt-0.5 -mr-1 rounded p-1 text-ink-3 transition-colors hover:bg-white/5 hover:text-ink-1"
            >
              <Icon name="close" size={13} />
            </button>
          </div>
        );
      })}
    </div>
  );
}