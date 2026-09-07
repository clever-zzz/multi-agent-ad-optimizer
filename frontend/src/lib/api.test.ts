import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, buildQuery, clearSession, request, setTokenListener, setTokens, system } from "@/lib/api";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

let calls: FetchCall[] = [];
let responder: (call: FetchCall, index: number) => Response | Promise<Response>;

beforeEach(() => {
  calls = [];
  responder = () => jsonResponse({ ok: true });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const call: FetchCall = { url: String(input), init };
      calls.push(call);
      return responder(call, calls.length - 1);
    }),
  );
  clearSession();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function headersOf(index: number): Record<string, string> {
  const headers = calls[index]?.init.headers as Record<string, string> | undefined;
  return headers ?? {};
}

describe("buildQuery", () => {
  it("serialises scalars and repeats array values", () => {
    expect(buildQuery({ page: 2, status: "open" })).toBe("?page=2&status=open");
    expect(buildQuery({ ids: ["a", "b"] })).toBe("?ids=a&ids=b");
  });

  it("drops null, undefined and empty strings so filters stay clean", () => {
    expect(buildQuery({ a: null, b: undefined, c: "", d: 0, e: false })).toBe("?d=0&e=false");
  });

  it("returns an empty string rather than a bare question mark", () => {
    expect(buildQuery({})).toBe("");
    expect(buildQuery(undefined)).toBe("");
  });

  it("URL-encodes values", () => {
    expect(buildQuery({ search: "q3 & q4" })).toBe("?search=q3+%26+q4");
  });
});

describe("request headers", () => {
  it("attaches the bearer token once a session exists", async () => {
    setTokens("access-123", "refresh-456");
    await api.get("/campaigns");
    expect(headersOf(0).Authorization).toBe("Bearer access-123");
  });

  it("omits Authorization when signed out", async () => {
    await api.get("/campaigns");
    expect(headersOf(0).Authorization).toBeUndefined();
  });

  it("always sends a request id so a failure can be traced to a log line", async () => {
    await api.get("/campaigns");
    expect(headersOf(0)["X-Request-ID"]).toMatch(/^(req_)?[0-9a-f-]{8,}/i);
  });

  it("keys mutations for idempotency but not reads", async () => {
    await api.get("/campaigns");
    await api.post("/campaigns", { name: "x" });
    expect(headersOf(0)["Idempotency-Key"]).toBeUndefined();
    expect(headersOf(1)["Idempotency-Key"]).toBeTruthy();
    expect(headersOf(1)["Content-Type"]).toBe("application/json");
  });

  it("does not set a content type on bodiless requests", async () => {
    await api.post("/actions/1/execute");
    expect(headersOf(0)["Content-Type"]).toBeUndefined();
  });
});

describe("error handling", () => {
  it("raises an ApiError carrying the RFC 9457 problem detail", async () => {
    responder = () =>
      jsonResponse(
        {
          type: "https://errors.adoptimizer/validation",
          title: "Validation failed",
          status: 422,
          detail: "daily_budget must be positive",
          errors: [{ loc: "daily_budget", msg: "must be greater than 0" }],
          request_id: "req-abc",
        },
        422,
        { "X-Request-ID": "req-abc" },
      );

    const error = await api.get("/campaigns").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.message).toBe("daily_budget must be positive");
    expect(apiError.requestId).toBe("req-abc");
    expect(apiError.fieldMessage("daily_budget")).toBe("must be greater than 0");
    expect(apiError.fieldMessage("name")).toBeUndefined();
  });

  it("still produces an ApiError when the body is not JSON", async () => {
    responder = () => new Response("gateway timeout", { status: 504, statusText: "Gateway Timeout" });
    const error = (await api.get("/campaigns").catch((caught: unknown) => caught)) as ApiError;
    expect(error.status).toBe(504);
    expect(error.message).toBe("Gateway Timeout");
  });

  it("returns undefined for 204 responses", async () => {
    responder = () => new Response(null, { status: 204 });
    await expect(api.del("/campaigns/1")).resolves.toBeUndefined();
  });
});

describe("token refresh on 401", () => {
  const rotated = {
    access_token: "access-new",
    refresh_token: "refresh-new",
    token_type: "bearer" as const,
    expires_in: 1800,
    user: {
      id: "u1",
      email: "ops@example.com",
      full_name: "Operator",
      role: "optimizer" as const,
      is_active: true,
      must_change_password: false,
      last_login_at: null,
      created_at: null,
    },
  };

  it("retries the original request exactly once after a successful rotation", async () => {
    setTokens("expired", "refresh-old");
    responder = (call) => {
      if (call.url.endsWith("/auth/refresh")) return jsonResponse(rotated);
      if (headersOf(calls.indexOf(call)).Authorization === "Bearer expired") {
        return jsonResponse({ title: "Token expired", status: 401 }, 401);
      }
      return jsonResponse({ items: [], total: 0, page: 1, page_size: 50 });
    };

    const result = await api.get<{ total: number }>("/campaigns");

    expect(result.total).toBe(0);
    expect(calls.map((call) => call.url)).toEqual([
      "/api/v1/campaigns",
      "/api/v1/auth/refresh",
      "/api/v1/campaigns",
    ]);
    expect(headersOf(2).Authorization).toBe("Bearer access-new");
  });

  it("clears the session and notifies the listener when rotation fails", async () => {
    setTokens("expired", "also-expired");
    const expired = vi.fn();
    setTokenListener(() => undefined, expired);

    responder = (call) =>
      call.url.endsWith("/auth/refresh")
        ? jsonResponse({ title: "Invalid refresh token", status: 401 }, 401)
        : jsonResponse({ title: "Token expired", status: 401 }, 401);

    await expect(api.get("/campaigns")).rejects.toBeInstanceOf(ApiError);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it("does not attempt a refresh when no refresh token is stored", async () => {
    setTokens("expired", null);
    responder = () => jsonResponse({ title: "Token expired", status: 401 }, 401);

    await expect(api.get("/campaigns")).rejects.toBeInstanceOf(ApiError);
    expect(calls).toHaveLength(1);
  });

  it("shares one refresh across concurrent 401s instead of rotating twice", async () => {
    setTokens("expired", "refresh-old");
    responder = async (call) => {
      if (call.url.endsWith("/auth/refresh")) {
        await new Promise((resolve) => setTimeout(resolve, 10));
        return jsonResponse(rotated);
      }
      if ((call.init.headers as Record<string, string>).Authorization === "Bearer expired") {
        return jsonResponse({ title: "Token expired", status: 401 }, 401);
      }
      return jsonResponse({ ok: true });
    };

    await Promise.all([api.get("/campaigns"), api.get("/runs"), api.get("/alerts")]);

    const refreshCalls = calls.filter((call) => call.url.endsWith("/auth/refresh"));
    expect(refreshCalls).toHaveLength(1);
  });
});

describe("request", () => {
  it("parses the JSON body of a successful call", async () => {
    responder = () => jsonResponse({ id: "c1", name: "Campaign" });
    await expect(request<{ id: string }>("/campaigns/c1")).resolves.toEqual({
      id: "c1",
      name: "Campaign",
    });
  });

  it("passes an abort signal through to fetch", async () => {
    const controller = new AbortController();
    await api.get("/campaigns", undefined, controller.signal);
    expect(calls[0]?.init.signal).toBe(controller.signal);
  });
});

describe("system probes", () => {
  it("targets the unversioned probe paths, never the /api/v1 prefix", async () => {
    responder = () => jsonResponse({ status: "ok" });

    await system.health<{ status: string }>();
    await system.ready<{ status: string }>();

    expect(calls).toHaveLength(2);
    expect(calls[0]?.url.endsWith("/healthz")).toBe(true);
    expect(calls[1]?.url.endsWith("/readyz")).toBe(true);
    for (const call of calls) {
      expect(call.url.includes("/api/v1")).toBe(false);
    }
  });

  it("leaves the bearer token off a public probe", async () => {
    setTokens("access-token", "refresh-token");
    responder = () => jsonResponse({ status: "ok" });

    await system.health<{ status: string }>();

    expect(headersOf(0).Authorization).toBeUndefined();
  });

  it("returns a 503 readiness answer instead of throwing", async () => {
    responder = () => jsonResponse({ status: "degraded", detail: "redis unreachable" }, 503);

    await expect(system.ready<{ status: string; detail: string }>()).resolves.toEqual({
      status: "degraded",
      detail: "redis unreachable",
    });
  });

  it("raises ApiError on an unexpected probe status", async () => {
    responder = () => jsonResponse({ title: "Boom", status: 500 }, 500);

    await expect(system.health<unknown>()).rejects.toBeInstanceOf(ApiError);
  });
});