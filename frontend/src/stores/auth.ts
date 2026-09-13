import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import { api, setTokenListener, setTokens } from "@/lib/api";
import type { Permission, Role, TokenResponse, User } from "@/lib/types";

const STORAGE_KEY = "adoptimizer.session";

type AuthStatus = "anonymous" | "authenticating" | "authenticated";

interface AuthState {
  user: User | null;
  status: AuthStatus;
  hydrated: boolean;
  refreshToken: string | null;
  login: (email: string, password: string) => Promise<User>;
  logout: (options?: { remote?: boolean }) => Promise<void>;
  hydrate: () => Promise<void>;
  refreshProfile: () => Promise<User | null>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  setUser: (user: User) => void;
  reset: () => void;
}

// Security note: only the *refresh* token is persisted; the access token stays in
// memory for the lifetime of the tab and is re-acquired on reload. That shrinks
// the XSS window compared with persisting both. Deployments behind nginx should
// migrate the refresh token into an httpOnly SameSite=Strict cookie; see
// docs/security.md.
interface PersistedSlice {
  user: User | null;
  status: AuthStatus;
  refreshToken: string | null;
}

// Mirrors _ROLE_PERMISSIONS in backend/src/adoptimizer/core/security.py. Admin is
// granted the full Permission enum server side, which is why "*" stands in here.
// Changing one side without the other shows operators controls the API rejects,
// so backend/tests/unit/test_security.py::TestFrontendMirror parses this object
// and fails when the two diverge.
const ROLE_PERMISSIONS: Record<Role, Permission[] | "*"> = {
  admin: "*",
  optimizer: [
    "campaign:read",
    "campaign:write",
    "run:read",
    "run:trigger",
    "action:approve",
    "action:execute",
    "alert:read",
    "alert:ack",
    "creative:write",
    "metrics:read",
    "system:read",
  ],
  analyst: [
    "campaign:read",
    "run:read",
    "alert:read",
    "alert:ack",
    "metrics:read",
    "system:read",
  ],
  viewer: ["campaign:read", "run:read", "alert:read", "metrics:read"],
  // A machine identity for the metrics pipeline. It signs in only if a human
  // picks the role by hand, and then sees the dashboard and nothing else.
  ingestor: ["metrics:read", "metrics:write"],
};

export function selectPermissions(user: User | null): Permission[] | "*" {
  if (!user) return [];
  return ROLE_PERMISSIONS[user.role] ?? [];
}

export function isAdmin(user: User | null): boolean {
  return user?.role === "admin";
}

export function can(user: User | null, permission: Permission): boolean {
  const granted = selectPermissions(user);
  if (granted === "*") return true;
  return granted.includes(permission);
}

// One in-flight rehydration, shared by every caller. See `hydrate` below.
let hydrationInFlight: Promise<void> | null = null;

export const useAuth = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      status: "anonymous",
      hydrated: false,
      refreshToken: null,

      setUser: (user) => set({ user }),

      login: async (email, password) => {
        const previous = get().status;
        set({ status: "authenticating" });
        try {
          const payload = await api.post<TokenResponse>("/auth/login", { email, password });
          setTokens(payload.access_token, payload.refresh_token);
          set({
            user: payload.user,
            status: "authenticated",
            hydrated: true,
            refreshToken: payload.refresh_token,
          });
          return payload.user;
        } catch (error) {
          set({ status: previous === "authenticated" ? "authenticated" : "anonymous" });
          throw error;
        }
      },

      logout: async (options) => {
        const remote = options?.remote ?? true;
        if (remote && get().status === "authenticated") {
          try {
            await api.post("/auth/logout");
          } catch {
            // A failed remote revoke must never trap the operator in the UI.
          }
        }
        get().reset();
      },

      hydrate: () => {
        // `/auth/refresh` rotates the refresh token, so two overlapping calls would
        // replay an already-consumed token and log the operator out. StrictMode runs
        // effects twice, and both the root guard and AppLayout ask for hydration, so
        // concurrent requests collapse onto a single in-flight promise.
        if (hydrationInFlight) return hydrationInFlight;
        const run = async (): Promise<void> => {
          const { status, refreshToken } = get();
          if (status !== "authenticated" || !refreshToken) {
            set({ hydrated: true, status: "anonymous", user: null, refreshToken: null });
            return;
          }
          try {
            const payload = await api.post<TokenResponse>("/auth/refresh", {
              refresh_token: refreshToken,
            });
            setTokens(payload.access_token, payload.refresh_token);
            set({
              user: payload.user,
              status: "authenticated",
              hydrated: true,
              refreshToken: payload.refresh_token,
            });
          } catch {
            setTokens(null, null);
            set({ hydrated: true, status: "anonymous", user: null, refreshToken: null });
          }
        };
        hydrationInFlight = run().finally(() => {
          hydrationInFlight = null;
        });
        return hydrationInFlight;
      },

      refreshProfile: async () => {
        if (get().status !== "authenticated") return null;
        try {
          const user = await api.get<User>("/auth/me");
          set({ user });
          return user;
        } catch {
          return null;
        }
      },

      changePassword: async (currentPassword, newPassword) => {
        await api.post("/auth/change-password", {
          current_password: currentPassword,
          new_password: newPassword,
        });
        const user = get().user;
        if (user) set({ user: { ...user, must_change_password: false } });
      },

      reset: () => {
        setTokens(null, null);
        set({ user: null, status: "anonymous", refreshToken: null, hydrated: true });
      },
    }),
    {
      name: STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state): PersistedSlice => ({
        user: state.user,
        status: state.status === "authenticating" ? "anonymous" : state.status,
        refreshToken: state.refreshToken,
      }),
      onRehydrateStorage: () => (state) => {
        // Restore the refresh token into the interceptor before the first request.
        if (state?.refreshToken) setTokens(null, state.refreshToken);
      },
    },
  ),
);

// Keep the fetch interceptor informed and force logout when a refresh fails.
setTokenListener(
  () => {
    /* Rotated access tokens live in memory only; nothing to persist. */
  },
  () => {
    useAuth.getState().reset();
  },
);