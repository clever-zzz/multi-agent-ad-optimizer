import { useI18n } from "@/i18n";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { useAuth } from "@/stores/auth";
import { toast } from "@/stores/toast";
import { useReadiness } from "@/hooks/useAdmin";
import { formatDuration } from "@/lib/format";

export interface TopbarProps {
  title: string;
  subtitle?: string;
  onMenu: () => void;
  onNewRun?: () => void;
  canTriggerRun?: boolean;
}

export function Topbar({ title, subtitle, onMenu, onNewRun, canTriggerRun = false }: TopbarProps) {
  const user = useAuth((state) => state.user);
  const logout = useAuth((state) => state.logout);
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const { data: health } = useReadiness({ refetchMs: 45_000 });
  const ready = health?.status === "ready";
  const degraded = Object.entries(health?.dependencies ?? {})
    .filter(([, state]) => {
      const status = (state as { status?: string }).status;
      return status === "unavailable" || status === "error";
    })
    .map(([name]) => name);

  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const { t, locale, setLocale } = useI18n();

  const handleLogout = async () => {
    setMenuOpen(false);
    await logout();
    toast.info(t("Signed out"), t("Your refresh token was revoked."));
    navigate("/login", { replace: true });
  };

  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-line bg-surface-1/92 px-4 backdrop-blur-md">
      <button
        type="button"
        onClick={onMenu}
        aria-label={t("Open navigation")}
        className="rounded-md p-1.5 text-ink-2 hover:bg-surface-3 hover:text-ink-1 lg:hidden"
      >
        <Icon name="menu" size={18} />
      </button>

      <div className="min-w-0 flex-1">
        <h1 className="truncate text-sm font-semibold text-ink-1">{title}</h1>
        {subtitle && <p className="truncate text-[11px] text-ink-3">{subtitle}</p>}
      </div>

      <div className="flex items-center gap-2">
        <div
          className="hidden items-center gap-2 rounded-lg border border-line bg-surface-2 px-2.5 py-1.5 sm:flex"
          title={
            degraded.length > 0
              ? t("Degraded: {list}", { list: degraded.join(", ") })
              : t("All critical dependencies healthy")
          }
        >
          <span
            className={cn(
              "size-2 rounded-full",
              ready ? "bg-pos live-dot" : health ? "bg-neg" : "bg-warn",
            )}
          />
          <span className="text-[11px] font-medium text-ink-2">
            {ready ? t("All systems ready") : health ? t("{count} degraded", { count: degraded.length }) : t("Checking…")}
          </span>
          {health && (
            <span className="tnum hidden text-[11px] text-ink-3 md:inline">
              {t("up {duration}", { duration: formatDuration(new Date(Date.now() - health.uptime_seconds * 1000).toISOString()) })}
            </span>
          )}
        </div>

        {canTriggerRun && onNewRun && (
          <Button variant="primary" icon="play" onClick={onNewRun}>
            {t("New run")}
          </Button>
        )}

        <button
          type="button"
          onClick={() => setLocale(locale === "zh-CN" ? "en" : "zh-CN")}
          aria-label={t("Switch language")}
          title={t("Switch language")}
          className="rounded-lg border border-line bg-surface-2 px-2 py-1.5 text-[11px] font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink-1"
        >
          {locale === "zh-CN" ? "EN" : "中文"}
        </button>

        <div className="relative" ref={menuRef}>
          <button
            type="button"
            onClick={() => setMenuOpen((value) => !value)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            className="flex items-center gap-2 rounded-lg border border-line bg-surface-2 py-1.5 pr-2 pl-1.5 transition-colors hover:bg-surface-3"
          >
            <span className="grid size-6 place-items-center rounded-full bg-brand-600/25 text-[11px] font-bold text-brand-300">
              {(user?.full_name || user?.email || "?").slice(0, 1).toUpperCase()}
            </span>
            <Icon name="chevronDown" size={13} className="text-ink-3" />
          </button>

          {menuOpen && (
            <div
              role="menu"
              className="card absolute right-0 z-30 mt-1.5 w-60 overflow-hidden shadow-2xl shadow-black/50"
            >
              <div className="border-b border-line px-3.5 py-3">
                <p className="truncate text-[13px] font-semibold text-ink-1">
                  {user?.full_name || t("Operator")}
                </p>
                <p className="truncate text-xs text-ink-3">{user?.email}</p>
                <div className="mt-2 flex items-center gap-1.5">
                  <Badge tone="brand">{t(user?.role ?? "unknown")}</Badge>
                  {user?.must_change_password && <Badge tone="warning">{t("password reset due")}</Badge>}
                </div>
              </div>
              <div className="p-1.5">
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuOpen(false);
                    navigate("/settings");
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-[13px] text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink-1"
                >
                  <Icon name="sliders" size={15} />
                  {t("System settings")}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => void handleLogout()}
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-[13px] text-neg transition-colors hover:bg-neg/10"
                >
                  <Icon name="logout" size={15} />
                  {t("Sign out")}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}