"""Error model.

Every error returned to clients uses the OpenAI error envelope:

    {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class GatewayError(Exception):
    """Base error carrying an OpenAI-style error payload."""

    def __init__(
        self,
        status_code: int = 500,
        message: str = "The gateway encountered an unexpected error.",
        error_type: str = "api_error",
        code: str | None = None,
        param: str | None = None,
        retryable: bool = False,
        fallback: bool = False,
        provider: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code
        self.param = param
        self.retryable = retryable
        self.fallback = fallback
        self.provider = provider
        self.headers: dict[str, str] = headers or {}

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }

    def to_response(self, headers: dict[str, str] | None = None) -> JSONResponse:
        merged = {**self.headers, **(headers or {})}
        return JSONResponse(
            status_code=self.status_code,
            content=self.payload(),
            headers=merged,
        )


class ProviderError(GatewayError):
    """Error raised by an upstream provider (or while talking to it)."""


class GuardrailBlocked(GatewayError):
    """A guardrail blocked the request or response."""

    def __init__(self, guardrail: str, reason: str) -> None:
        super().__init__(
            status_code=400,
            message=f"Request blocked by guardrail '{guardrail}': {reason}",
            error_type="content_policy_violation",
            code="content_policy_violation",
        )
        self.guardrail = guardrail
        self.reason = reason


class AuthError(GatewayError):
    def __init__(self, message: str = "Incorrect API key provided.") -> None:
        super().__init__(
            status_code=401,
            message=message,
            error_type="invalid_request_error",
            code="invalid_api_key",
        )


class RateLimitError(GatewayError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(
            status_code=429,
            message=message,
            error_type="requests",
            code="rate_limit_exceeded",
        )
        self.retry_after = retry_after
        if retry_after is not None and retry_after > 0:
            # RFC 7231: integer seconds until the client should retry.
            self.headers["Retry-After"] = str(int(retry_after) + 1)


class BudgetExceededError(GatewayError):
    def __init__(
        self,
        key_name: str,
        budget: float,
        spent: float,
        period: str = "total",
        kind: str = "key",
    ) -> None:
        # period: "total" (lifetime max_budget_usd) | "daily" | "monthly"
        # (calendar-window caps). kind: "key" or "tenant" — tenants pool
        # every member key's spend, so the message says which was
        # exhausted. Same 429/insufficient_quota semantics either way.
        what = "tenant" if kind == "tenant" else "virtual key"
        if period == "total":
            message = (
                f"{what.capitalize()} '{key_name}' has exceeded its budget "
                f"(${spent:.4f} spent of ${budget:.2f} limit)."
            )
        else:
            message = (
                f"{what.capitalize()} '{key_name}' has exceeded its {period} budget "
                f"(${spent:.4f} spent of ${budget:.2f} {period} limit)."
            )
        super().__init__(
            status_code=429,
            message=message,
            error_type="insufficient_quota",
            code="insufficient_quota",
        )


class ModelNotAllowedError(GatewayError):
    def __init__(self, model: str) -> None:
        super().__init__(
            status_code=404,
            message=f"The model '{model}' does not exist or is not available for this API key.",
            error_type="invalid_request_error",
            code="model_not_found",
            param="model",
        )


class ModelNotFoundError(GatewayError):
    def __init__(self, model: str) -> None:
        super().__init__(
            status_code=404,
            message=f"The model '{model}' does not exist.",
            error_type="invalid_request_error",
            code="model_not_found",
            param="model",
        )
