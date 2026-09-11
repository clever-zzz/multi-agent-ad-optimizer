import { useI18n } from "@/i18n";
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { useAuth, can, isAdmin } from "@/stores/auth";
import type { Permission } from "@/lib/types";

export interface ProtectedRouteProps {
  children: ReactNode;
  permission?: Permission;
  adminOnly?: boolean;
}

// Route guard. Authentication state is rehydrated asynchronously, so the guard
// shows a spinner instead of flashing the login page on every hard reload.
export function ProtectedRoute({ children, permission, adminOnly = false }: ProtectedRouteProps) {
  const status = useAuth((state) => state.status);
  const hydrated = useAuth((state) => state.hydrated);
  const user = useAuth((state) => state.user);
  const location = useLocation();
  const { t } = useI18n();

  if (!hydrated) {
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner label={t("Restoring session…")} />
      </div>
    );
  }

  if (status !== "authenticated" || !user) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }

  if (adminOnly && !isAdmin(user)) {
    return (
      <div className="grid min-h-[60vh] place-items-center">
        <EmptyState
          tone="negative"
          icon="shield"
          title={t("Administrator access required")}
          hint={t("This area reads the audit trail and runtime configuration, which the API restricts to the admin role.")}
        />
      </div>
    );
  }

  if (permission && !can(user, permission)) {
    return (
      <div className="grid min-h-[60vh] place-items-center">
        <EmptyState
          tone="negative"
          icon="shield"
          title={t("Insufficient permissions")}
          hint={t("Your role ({role}) is not granted {permission}.", { role: user.role, permission })}
        />
      </div>
    );
  }

  return <>{children}</>;
}