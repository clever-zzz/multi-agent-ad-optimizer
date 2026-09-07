"""HTTP middleware: correlation ids, access logs, security headers, limits."""

from __future__ import annotations

import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .config import Settings
from .errors import RateLimitedError, problem_response
from .logging import bind_request_context, clear_request_context, get_logger
from .metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
EXEMPT_PATHS = frozenset({"/healthz", "/livez", "/readyz", "/metrics", "/favicon.ico"})


def _route_template(request: Request) -> str:
    """Return the parameterised route so metric labels stay low-cardinality."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return request.url.path


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Propagate a correlation id and bind logging context for the request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        if len(request_id) > 128:
            request_id = request_id[:128]

        request.state.request_id = request_id
        bind_request_context(request_id=request_id)

        started = time.perf_counter()
        status_code = 500
        response: Response | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(request)

            if request.url.path not in EXEMPT_PATHS:
                HTTP_REQUESTS_TOTAL.labels(
                    method=request.method,
                    route=route,
                    status_class=str(status_code // 100) + "xx",
                ).inc()
                HTTP_REQUEST_DURATION_SECONDS.labels(method=request.method, route=route).observe(
                    elapsed
                )

                logger.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    route=route,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 2),
                    client=request.client.host if request.client else None,
                )

            if response is not None:
                response.headers[REQUEST_ID_HEADER] = request_id
            clear_request_context()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply a conservative baseline of response security headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class InMemoryRateLimiter:
    """Fixed-window limiter for a single API process.

    Buckets live in process memory, so the effective ceiling is
    ``limit x replicas``. Multi-replica deployments that need a global ceiling
    must put the limiter behind a shared store; see
    docs/production/08-limitations-and-roadmap.md.
    """

    def __init__(self) -> None:
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        """Return (allowed, seconds_until_reset) for a key."""
        now = int(time.time())
        window_start = now - (now % window_seconds)
        bucket = self._windows.get(key)

        if bucket is None or bucket[0] != window_start:
            self._windows[key] = (window_start, 1)
            self._prune(now)
            return True, window_seconds

        count = bucket[1]
        if count >= limit:
            return False, max(1, window_start + window_seconds - now)

        self._windows[key] = (window_start, count + 1)
        return True, window_seconds

    def _prune(self, now: int) -> None:
        """Drop stale buckets so memory stays bounded."""
        if len(self._windows) < 4096:
            return
        cutoff = now - 300
        stale = [k for k, (start, _) in self._windows.items() if start < cutoff]
        for key in stale:
            self._windows.pop(key, None)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Throttle authenticated write traffic; anonymous reads are limited too."""

    def __init__(self, app: Any, settings: Settings, limiter: InMemoryRateLimiter) -> None:
        super().__init__(app)
        self._settings = settings
        self._limiter = limiter

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cfg = self._settings.rate_limit
        if not cfg.enabled or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        identity = "anon"
        state_user = getattr(request.state, "principal_id", None)
        if state_user:
            identity = str(state_user)
        elif request.client:
            identity = request.client.host or "anon"

        is_write = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        limit = cfg.write_requests_per_minute if is_write else cfg.default_requests_per_minute
        key = "rl:" + ("w" if is_write else "r") + ":" + identity

        allowed, retry_after = self._limiter.allow(key, limit)
        if not allowed:
            error = RateLimitedError(
                "Rate limit exceeded, please retry shortly",
                detail={"limit_per_minute": limit},
            )
            error.headers = {"Retry-After": str(retry_after)}
            logger.warning("rate_limited", identity=identity, path=request.url.path)
            return problem_response(error, request_id=getattr(request.state, "request_id", None))

        return await call_next(request)
