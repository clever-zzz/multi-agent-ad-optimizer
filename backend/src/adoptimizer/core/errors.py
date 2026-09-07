"""Domain error hierarchy and HTTP error rendering.

Errors surface as RFC 9457 "application/problem+json" documents so clients can
branch on a stable type URI instead of parsing free-form text.
"""

from __future__ import annotations

from typing import Any

import orjson
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging import get_logger

logger = get_logger(__name__)

PROBLEM_BASE = "https://adoptimizer.dev/errors"
PROBLEM_MEDIA_TYPE = "application/problem+json"

_STATUS_TO_CODE: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    402: "budget_exceeded",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_failed",
    429: "rate_limited",
    503: "dependency_unavailable",
}


class AdoptimizerError(Exception):
    """Base class for all application-level failures."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "internal_error"
    title: str = "Internal Server Error"

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.title
        self.detail = detail or {}
        self.headers = headers or {}
        super().__init__(self.message)

    @property
    def type_uri(self) -> str:
        return PROBLEM_BASE + "/" + self.error_code


class NotFoundError(AdoptimizerError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"
    title = "Resource Not Found"


class ConflictError(AdoptimizerError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "conflict"
    title = "Resource Conflict"


class ValidationFailure(AdoptimizerError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "validation_failed"
    title = "Validation Failed"


class AuthenticationError(AdoptimizerError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "unauthenticated"
    title = "Authentication Required"

    def __init__(self, message: str | None = None, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, detail=detail, headers={"WWW-Authenticate": "Bearer"})


class PermissionDeniedError(AdoptimizerError):
    status_code = status.HTTP_403_FORBIDDEN
    error_code = "permission_denied"
    title = "Permission Denied"


class RateLimitedError(AdoptimizerError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "rate_limited"
    title = "Too Many Requests"


class DependencyUnavailableError(AdoptimizerError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "dependency_unavailable"
    title = "Dependency Unavailable"


class RunInProgressError(ConflictError):
    error_code = "run_in_progress"
    title = "Optimization Run Already In Progress"


class BudgetExceededError(AdoptimizerError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    error_code = "budget_exceeded"
    title = "LLM Spend Budget Exceeded"


class ActionRequiresApprovalError(AdoptimizerError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "approval_required"
    title = "Action Requires Approval"


class ExternalServiceError(AdoptimizerError):
    status_code = status.HTTP_502_BAD_GATEWAY
    error_code = "external_service_error"
    title = "Upstream Service Error"


def problem_response(error: AdoptimizerError, *, request_id: str | None = None) -> Response:
    """Render a domain error as a Problem Details document."""
    body: dict[str, Any] = {
        "type": error.type_uri,
        "title": error.title,
        "status": error.status_code,
        "detail": error.message,
        "code": error.error_code,
    }
    if error.detail:
        body["errors"] = error.detail
    if request_id:
        body["request_id"] = request_id
    # A JSONResponse subclass would advertise "application/json" and contradict
    # the documented RFC 9457 contract, so serialise explicitly with the problem
    # media type. Clients key off ``code``/``type``, never off free-form text.
    return Response(
        content=orjson.dumps(body),
        status_code=error.status_code,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=error.headers or None,
    )


def _request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers translating every failure mode into Problem Details."""

    @app.exception_handler(AdoptimizerError)
    async def _handle_domain_error(request: Request, exc: AdoptimizerError) -> Response:
        logger.warning(
            "domain_error", code=exc.error_code, detail=exc.message, path=request.url.path
        )
        return problem_response(exc, request_id=_request_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(request: Request, exc: RequestValidationError) -> Response:
        errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", [])[1:]),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        failure = ValidationFailure("Request payload failed validation", detail={"fields": errors})
        logger.info("request_validation_failed", path=request.url.path, count=len(errors))
        return problem_response(failure, request_id=_request_id_of(request))

    @app.exception_handler(ValidationError)
    async def _handle_pydantic_validation(request: Request, exc: ValidationError) -> Response:
        failure = ValidationFailure(
            "Data failed validation",
            detail={"fields": [{"message": e.get("msg", "")} for e in exc.errors()]},
        )
        return problem_response(failure, request_id=_request_id_of(request))

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        error = AdoptimizerError(str(exc.detail))
        error.status_code = exc.status_code
        if isinstance(exc.detail, str):
            error.title = exc.detail
        error.error_code = _STATUS_TO_CODE.get(exc.status_code, "http_error")
        return problem_response(error, request_id=_request_id_of(request))

    @app.exception_handler(IntegrityError)
    async def _handle_integrity_error(request: Request, exc: IntegrityError) -> Response:
        logger.warning("database_integrity_error", path=request.url.path, error=str(exc.orig))
        conflict = ConflictError("The operation violates a uniqueness or foreign-key constraint")
        return problem_response(conflict, request_id=_request_id_of(request))

    @app.exception_handler(SQLAlchemyError)
    async def _handle_database_error(request: Request, exc: SQLAlchemyError) -> Response:
        logger.error("database_error", path=request.url.path, error=str(exc), exc_info=True)
        unavailable = DependencyUnavailableError("The database is temporarily unavailable")
        return problem_response(unavailable, request_id=_request_id_of(request))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> Response:
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )
        return problem_response(
            AdoptimizerError("An unexpected error occurred"),
            request_id=_request_id_of(request),
        )
