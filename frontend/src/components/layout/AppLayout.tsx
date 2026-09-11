import { useI18n } from "@/i18n";
import { useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { Sidebar } from "@/components/layout/Sidebar";
import { Topbar } from "@/components/layout/Topbar";
import { Toaster } from "@/components/ui/Toaster";
import { Spinner } from "@/components/ui/Spinner";
import { RunTriggerDialog } from "@/components/RunTriggerDialog";
import { ChangePasswordDialog } from "@/components/ChangePasswordDialog";
import { useAuth, can } from "@/stores/auth";
import { useActions } from "@/hooks/useActions";
import { useAlertSummary } from "@/hooks/useAlerts";

interface RouteMeta {
  prefix: string;
  title: string;
  subtitle: string;
  showRunButton: boolean;
}

const ROUTE_META: RouteMeta[] = [
  {
    prefix: "/dashboard",
    title: "Dashboard",
    subtitle: "Portfolio health, delivery trend and outstanding work",
    showRunButton: true,
  },
  {
    prefix: "/campaigns",
    title: "Campaigns",
    subtitle: "Budgets, targets and live delivery per campaign",
    showRunButton: true,
  },
  {
    prefix: "/runs",
    title: "Optimization Runs",
    subtitle: "Agent loop executions with live progress and outcomes",
    showRunButton: true,
  },
  {
    prefix: "/actions",
    title: "Action Approvals",
    subtitle: "Human-in-the-loop gate before anything touches a platform",
    showRunButton: false,
  },
  {
    prefix: "/alerts",
    title: "Alerts",
    subtitle: "Anomalies raised by the monitor agent",
    showRunButton: false,
  },
  {
    prefix: "/creatives",
    title: "Creatives",
    subtitle: "Human and model-generated copy, scored on live performance",
    showRunButton: false,
  },
  {
    prefix: "/experiments",
    title: "Experiments",
    subtitle: "A/B tests with sequential stopping and power analysis",
    showRunButton: false,
  },
  {
    prefix: "/audit",
    title: "Audit Log",
    subtitle: "Immutable record of every privileged operation",
    showRunButton: false,
  },
  {
    prefix: "/settings",
    title: "System",
    subtitle: "Runtime configuration, dependency health and model spend",
    showRunButton: false,
  },
];

const FALLBACK_META: RouteMeta = {
  prefix: "/",
  title: "Ad Optimizer",
  subtitle: "Multi-agent advertising optimization console",
  showRunButton: false,
};

export function AppLayout() {
  const location = useLocation();
  const { t } = useI18n();
  const navigate = useNavigate();
  const user = useAuth((state) => state.user);
  const status = useAuth((state) => state.status);
  const hydrated = useAuth((state) => state.hydrated);
  const hydrate = useAuth((state) => state.hydrate);

  const [navOpen, setNavOpen] = useState(false);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);

  useEffect(() => {
    if (!hydrated) void hydrate();
  }, [hydrated, hydrate]);

  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (hydrated && status === "anonymous") {
      navigate("/login", { replace: true, state: { from: location.pathname } });
    }
  }, [hydrated, status, navigate, location.pathname]);

  useEffect(() => {
    if (user?.must_change_password) setPasswordDialogOpen(true);
  }, [user?.must_change_password]);

  const meta = useMemo(() => {
    // Longest matching prefix wins so /campaigns/:id keeps the Campaigns header.
    const match = ROUTE_META.filter((item) => location.pathname.startsWith(item.prefix)).sort(
      (a, b) => b.prefix.length - a.prefix.length,
    )[0];
    return match ?? FALLBACK_META;
  }, [location.pathname]);

  const pending = useActions({ page: 1, pageSize: 1, status: "proposed" });
  const alertSummary = useAlertSummary();

  if (!hydrated || status === "anonymous") {
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner label={hydrated ? t("Redirecting to sign in…") : t("Restoring session…")} />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen bg-surface-0">
      <Sidebar
        open={navOpen}
        onClose={() => setNavOpen(false)}
        pendingActions={pending.data?.total ?? 0}
        openAlerts={alertSummary.data?.open ?? 0}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar
          title={t(meta.title)}
          subtitle={t(meta.subtitle)}
          onMenu={() => setNavOpen(true)}
          onNewRun={() => setRunDialogOpen(true)}
          canTriggerRun={meta.showRunButton && can(user, "run:trigger")}
        />

        <main className="min-w-0 flex-1 px-4 py-4 sm:px-6 lg:px-7">
          <Outlet />
        </main>

        <footer className="border-t border-line px-6 py-3 text-[11px] text-ink-3">
          <span>
            {t("Every model-proposed change stays read-only until an operator approves it. Approval is audited.")}
          </span>
        </footer>
      </div>

      <RunTriggerDialog open={runDialogOpen} onClose={() => setRunDialogOpen(false)} />
      <ChangePasswordDialog
        open={passwordDialogOpen}
        onClose={() => setPasswordDialogOpen(false)}
        mandatory={Boolean(user?.must_change_password)}
      />
      <Toaster />
    </div>
  );
}