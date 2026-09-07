import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import App from "@/App";
import { clearSession } from "@/lib/api";
import { useAuth } from "@/stores/auth";
import type { TokenResponse, User } from "@/lib/types";

const OPERATOR: User = {
  id: "u-1",
  email: "ops@example.com",
  full_name: "Operator",
  role: "admin",
  is_active: true,
  must_change_password: false,
  last_login_at: null,
  created_at: null,
};

function tokenResponse(): TokenResponse {
  return {
    access_token: "at-2",
    refresh_token: "rt-2",
    token_type: "bearer",
    expires_in: 900,
    user: OPERATOR,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let urls: string[] = [];

function renderAppAt(path: string): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  urls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url.includes("/auth/refresh")) return jsonResponse(tokenResponse());
      return jsonResponse({ status: "ready", dependencies: {} });
    }),
  );
  localStorage.clear();
  clearSession();
  useAuth.setState({ user: null, status: "anonymous", hydrated: false, refreshToken: null });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("session rehydration", () => {
  // Regression. Hydration used to be triggered only from AppLayout, which
  // ProtectedRoute refuses to render until `hydrated` flips. Neither could make
  // progress without the other, so a hard reload of any protected URL hung on
  // "Restoring session…" forever and the operator never reached the sign-in page.
  it("resolves a hard reload of a protected route instead of hanging on the spinner", async () => {
    renderAppAt("/dashboard");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    // Spinner renders its label twice (visible <p> plus an sr-only span).
    expect(screen.queryAllByText("Restoring session…")).toHaveLength(0);
  });

  // `/auth/refresh` rotates the refresh token. StrictMode runs effects twice and
  // two components ask for hydration, so overlapping calls must share one request
  // or the second one replays a consumed token and forces a logout.
  it("collapses concurrent hydrate calls onto a single refresh request", async () => {
    useAuth.setState({
      user: OPERATOR,
      status: "authenticated",
      hydrated: false,
      refreshToken: "rt-1",
    });

    await Promise.all([useAuth.getState().hydrate(), useAuth.getState().hydrate()]);

    expect(urls.filter((url) => url.includes("/auth/refresh"))).toHaveLength(1);
    const state = useAuth.getState();
    expect(state.hydrated).toBe(true);
    expect(state.status).toBe("authenticated");
    expect(state.refreshToken).toBe("rt-2");
  });

  it("falls back to anonymous when the stored refresh token is rejected", async () => {
    useAuth.setState({
      user: OPERATOR,
      status: "authenticated",
      hydrated: false,
      refreshToken: "rt-stale",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return jsonResponse({ type: "about:blank", title: "Unauthorized", status: 401 }, 401);
      }),
    );

    await useAuth.getState().hydrate();

    const state = useAuth.getState();
    expect(state.hydrated).toBe(true);
    expect(state.status).toBe("anonymous");
    expect(state.refreshToken).toBeNull();
  });
});