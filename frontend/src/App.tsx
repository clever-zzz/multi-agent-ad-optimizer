import { Suspense, lazy, useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AppLayout } from "@/components/layout/AppLayout";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Spinner } from "@/components/ui/Spinner";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { useAuth } from "@/stores/auth";

// Route-level code splitting: the operator console loads the dashboard first and
// pulls governance views only when someone navigates to them.
const DashboardPage = lazy(() => import("@/pages/DashboardPage").then((m) => ({ default: m.DashboardPage })));
const CampaignsPage = lazy(() => import("@/pages/CampaignsPage").then((m) => ({ default: m.CampaignsPage })));
const CampaignDetailPage = lazy(() =>
  import("@/pages/CampaignDetailPage").then((m) => ({ default: m.CampaignDetailPage })),
);
const RunsPage = lazy(() => import("@/pages/RunsPage").then((m) => ({ default: m.RunsPage })));
const RunDetailPage = lazy(() => import("@/pages/RunDetailPage").then((m) => ({ default: m.RunDetailPage })));
const ActionsPage = lazy(() => import("@/pages/ActionsPage").then((m) => ({ default: m.ActionsPage })));
const AlertsPage = lazy(() => import("@/pages/AlertsPage").then((m) => ({ default: m.AlertsPage })));
const CreativesPage = lazy(() => import("@/pages/CreativesPage").then((m) => ({ default: m.CreativesPage })));
const ExperimentsPage = lazy(() => import("@/pages/ExperimentsPage").then((m) => ({ default: m.ExperimentsPage })));
const AuditPage = lazy(() => import("@/pages/AuditPage").then((m) => ({ default: m.AuditPage })));
const SettingsPage = lazy(() => import("@/pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));

function PageFallback() {
  return (
    <div className="grid min-h-[50vh] place-items-center">
      <Spinner size={22} label="Loading view…" />
    </div>
  );
}

export default function App() {
  const location = useLocation();
  const hydrated = useAuth((state) => state.hydrated);
  const hydrate = useAuth((state) => state.hydrate);

  // Session rehydration has to be triggered above the route guards. ProtectedRoute
  // renders a spinner instead of its children until `hydrated` flips, so a trigger
  // living inside the guarded subtree never runs and a hard reload of any protected
  // URL hangs on "Restoring session…" forever.
  useEffect(() => {
    if (!hydrated) void hydrate();
  }, [hydrated, hydrate]);

  return (
    <ErrorBoundary resetKey={location.pathname}>
      <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />

          <Route
            element={
              <ProtectedRoute>
                <AppLayout />
              </ProtectedRoute>
            }
          >
            <Route path="/dashboard" element={<DashboardPage />} />

            <Route
              path="/campaigns"
              element={
                <ProtectedRoute permission="campaign:read">
                  <CampaignsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/campaigns/:campaignId"
              element={
                <ProtectedRoute permission="campaign:read">
                  <CampaignDetailPage />
                </ProtectedRoute>
              }
            />

            <Route
              path="/runs"
              element={
                <ProtectedRoute permission="run:read">
                  <RunsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/runs/:runId"
              element={
                <ProtectedRoute permission="run:read">
                  <RunDetailPage />
                </ProtectedRoute>
              }
            />

            <Route
              path="/actions"
              element={
                <ProtectedRoute permission="run:read">
                  <ActionsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/alerts"
              element={
                <ProtectedRoute permission="alert:read">
                  <AlertsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/creatives"
              element={
                <ProtectedRoute permission="campaign:read">
                  <CreativesPage />
                </ProtectedRoute>
              }
            />

            <Route
              path="/experiments"
              element={
                <ProtectedRoute adminOnly>
                  <ExperimentsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/audit"
              element={
                <ProtectedRoute adminOnly>
                  <AuditPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/settings"
              element={
                <ProtectedRoute adminOnly>
                  <SettingsPage />
                </ProtectedRoute>
              }
            />

            <Route path="*" element={<NotFoundPage />} />
          </Route>

          <Route path="/" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}