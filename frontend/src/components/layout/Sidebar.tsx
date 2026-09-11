import { useI18n } from "@/i18n";
import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";
import { Icon } from "@/components/ui/Icon";
import { GROUP_LABELS, visibleNav } from "@/lib/navigation";
import { useAuth } from "@/stores/auth";

export interface SidebarProps {
  open: boolean;
  onClose: () => void;
  pendingActions?: number;
  openAlerts?: number;
}

export function Sidebar({ open, onClose, pendingActions = 0, openAlerts = 0 }: SidebarProps) {
  const user = useAuth((state) => state.user);
  const { t } = useI18n();
  const nav = visibleNav(user);

  const badgeFor = (to: string): number => {
    if (to === "/actions") return pendingActions;
    if (to === "/alerts") return openAlerts;
    return 0;
  };

  return (
    <>
      <div
        className={cn(
          "fixed inset-0 z-30 bg-black/60 transition-opacity lg:hidden",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={onClose}
        role="presentation"
      />
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-60 shrink-0 flex-col border-r border-line bg-surface-1",
          "transition-transform duration-200 lg:static lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-14 items-center gap-2.5 border-b border-line px-4">
          <span className="grid size-8 place-items-center rounded-lg bg-gradient-to-br from-brand-500 to-accent-400 text-white shadow-md shadow-brand-600/25">
            <Icon name="target" size={17} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[13px] leading-tight font-semibold text-ink-1">
              AdOptimizer
            </p>
            <p className="truncate text-[11px] leading-tight text-ink-3">{t("Multi-agent console")}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("Close navigation")}
            className="rounded-md p-1 text-ink-3 hover:bg-surface-3 hover:text-ink-1 lg:hidden"
          >
            <Icon name="close" size={16} />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-2.5 py-3">
          {(["operate", "govern"] as const).map((group) => {
            const items = nav[group];
            if (items.length === 0) return null;
            return (
              <div key={group} className="mb-4">
                <p className="px-2.5 pb-1.5 text-[10px] font-semibold tracking-[0.14em] text-ink-3 uppercase">
                  {t(GROUP_LABELS[group])}
                </p>
                <ul className="flex flex-col gap-0.5">
                  {items.map((item) => {
                    const count = badgeFor(item.to);
                    return (
                      <li key={item.to}>
                        <NavLink
                          to={item.to}
                          onClick={onClose}
                          className={({ isActive }) =>
                            cn(
                              "group flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium transition-colors",
                              isActive
                                ? "bg-brand-600/15 text-brand-300 shadow-[inset_2px_0_0_0_var(--color-brand-500)]"
                                : "text-ink-2 hover:bg-surface-2 hover:text-ink-1",
                            )
                          }
                        >
                          <Icon name={item.icon} size={16} className="shrink-0 opacity-85" />
                          <span className="min-w-0 flex-1 truncate">{t(item.label)}</span>
                          {count > 0 && (
                            <span className="tnum rounded-full bg-neg/20 px-1.5 py-px text-[10px] font-bold text-neg">
                              {count > 99 ? "99+" : count}
                            </span>
                          )}
                        </NavLink>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </nav>

        <div className="border-t border-line px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="grid size-7 shrink-0 place-items-center rounded-full bg-surface-3 text-[11px] font-bold text-brand-300">
              {(user?.full_name || user?.email || "?").slice(0, 1).toUpperCase()}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium text-ink-1">
                {user?.full_name || user?.email || t("Signed out")}
              </p>
              <p className="truncate text-[11px] text-ink-3 capitalize">{t(user?.role ?? "—")}</p>
            </div>
          </div>
        </div>
      </aside>
    </>
  );
}