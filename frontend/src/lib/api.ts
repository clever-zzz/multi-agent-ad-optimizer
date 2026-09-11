import type { ProblemDetail, TokenResponse } from "@/lib/types";

import { tStatic } from "@/i18n/translate";

// Empty base URL means "same origin", which in development is the Vite proxy and
// in production is nginx. No backend host is ever baked into the client bundle.
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");
export const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly requestId: string;
  readonly fieldErrors: Array<{ loc: string; msg: string; type?: string }>;

  constructor(problem: ProblemDetail, fallbackTitle?: string) {
    super(problem.detail || problem.title || fallbackTitle || tStatic("Request failed"));
    this.name = "ApiError";
    this.status = problem.status;
    this.type = problem.type;
    this.requestId = problem.request_id ?? "";
    this.fieldErrors = problem.errors ?? [];
  }

  fieldMessage(loc: string): string | undefined {
    return this.fieldErrors.find((e) => e.loc === loc)?.msg;
  }
}

// --- Token plumbing -------------------------------------------------------
// The auth store owns persistence; this module owns the in-memory copy used by
// the fetch interceptor. Keeping them separate avoids an import cycle.

let accessToken: string | null = null;
let refreshToken: string | null = null;
let onTokensChanged: ((tokens: { access_token: string; refresh_token: string } | null) => void) | null =
  null;
let onSessionExpired: (() => void) | null = null;

export function setTokenListener(
  changed: (tokens: { access_token: string; refresh_token: string } | null) => void,
  expired: () => void,
): void {
  onTokensChanged = changed;
  onSessionExpired = expired;
}

export function setTokens(access: string | null, refresh: string | null): void {
  accessToken = access;
  refreshToken = refresh;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function clearSession(): void {
  accessToken = null;
  refreshToken = null;
  onTokensChanged?.(null);
}

// --- Helpers --------------------------------------------------------------

export function newRequestId(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
    return cryptoRef.randomUUID();
  }
  return `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

type QueryValue = string | number | boolean | null | undefined | Array<string | number>;

export function buildQuery(params: Record<string, QueryValue> | undefined): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, String(item));
      continue;
    }
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

async function parseProblem(response: Response): Promise<ProblemDetail> {
  const requestId = response.headers.get("X-Request-ID") ?? "";
  try {
    const body = (await response.json()) as Partial<ProblemDetail>;
    return {
      type: body.type ?? "about:blank",
      title: body.title ?? response.statusText,
      status: body.status ?? response.status,
      detail: body.detail,
      instance: body.instance,
      errors: body.errors,
      request_id: body.request_id ?? requestId,
    };
  } catch {
    return {
      type: "about:blank",
      title: response.statusText || tStatic("Request failed"),
      status: response.status,
      request_id: requestId,
    };
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
  // Mutations are idempotency-keyed so a retried click cannot double-apply.
  idempotent?: boolean;
  headers?: Record<string, string>;
  raw?: boolean;
}

let refreshInFlight: Promise<boolean> | null = null;

async function performRefresh(): Promise<boolean> {
  if (!refreshToken) return false;
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${API_BASE}${API_PREFIX}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Request-ID": newRequestId() },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!response.ok) return false;
      const payload = (await response.json()) as TokenResponse;
      accessToken = payload.access_token;
      refreshToken = payload.refresh_token;
      onTokensChanged?.({
        access_token: payload.access_token,
        refresh_token: payload.refresh_token,
      });
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

async function send(path: string, options: RequestOptions, isRetry: boolean): Promise<Response> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-Request-ID": newRequestId(),
    ...options.headers,
  };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotent && method !== "GET") headers["Idempotency-Key"] = newRequestId();

  return fetch(`${API_BASE}${API_PREFIX}${path}${buildQuery(options.query)}`, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  }).then(async (response) => {
    if (response.status === 401 && !isRetry) {
      const refreshed = await performRefresh();
      if (refreshed) return send(path, options, true);
      clearSession();
      onSessionExpired?.();
    }
    return response;
  });
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options, false);
  if (!response.ok) throw new ApiError(await parseProblem(response));
  if (options.raw) return (await response.text()) as unknown as T;
  if (response.status === 204) return undefined as unknown as T;
  const text = await response.text();
  if (!text) return undefined as unknown as T;
  return JSON.parse(text) as T;
}

// --- Endpoint surface -----------------------------------------------------
// Typed wrappers keep pages free of string concatenation.

export const api = {
  get: <T>(path: string, query?: Record<string, QueryValue>, signal?: AbortSignal) =>
    request<T>(path, { method: "GET", query, signal }),
  post: <T>(path: string, body?: unknown, query?: Record<string, QueryValue>) =>
    request<T>(path, { method: "POST", body, query, idempotent: true }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body, idempotent: true }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body, idempotent: true }),
  del: <T>(path: string, query?: Record<string, QueryValue>) =>
    request<T>(path, { method: "DELETE", query, idempotent: true }),
};

// Liveness and readiness probes live outside /api/v1 and are unauthenticated,
// so they bypass `request` (which prefixes the API version and attaches the
// bearer token) and hit the absolute path instead.
async function probe<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json", "X-Request-ID": newRequestId() },
  });
  // /readyz answers 503 while a dependency is down; that is a valid reply, not
  // a transport error, so the caller can render it.
  if (response.status !== 200 && response.status !== 503) {
    throw new ApiError(await parseProblem(response));
  }
  return (await response.json()) as T;
}

export const system = {
  health: <T>() => probe<T>("/healthz"),
  ready: <T>() => probe<T>("/readyz"),
};