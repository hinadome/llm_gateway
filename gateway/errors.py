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


class BudgetProjectionError(GatewayError):
    """The tier-50 trending denial: the caller is NOT over its budget
    — it is trending over. The month-end run-rate projection exceeds
    the monthly budget, so the request is denied before the money is
    spent. Same 429 / insufficient_quota semantics as
    BudgetExceededError — a caller cannot and should not distinguish
    the shapes — but the message says it is the PROJECTION, with the
    numbers (projected month-end, spent so far, day of month), and
    the denial self-heals: spend over elapsed days is a ratio, so a
    caller that genuinely stops spending watches its projection decay
    toward the actual as the month elapses and traffic resumes — the
    hard cap stays the wall it always was."""

    def __init__(
        self,
        key_name: str,
        *,
        budget: float,
        spent: float,
        projected_usd: float,
        elapsed_days: float,
        days_in_month: float,
        kind: str = "key",
    ) -> None:
        what = "tenant" if kind == "tenant" else "virtual key"
        message = (
            f"{what.capitalize()} '{key_name}' is projected to spend "
            f"${projected_usd:.2f} by month end (day {elapsed_days:.1f} of "
            f"{days_in_month:.0f}), over its ${budget:.2f} monthly budget "
            f"(${spent:.4f} spent so far)."
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


class ToolsNotAllowedError(GatewayError):
    """Tool-call policy (tier 30): the request declares tools this key
    (or its tenant) may not use. Unlike model permission this is a 400
    naming the offending tools — the caller sent them, so there is no
    existence to leak and a debuggable error beats a mysterious one."""

    def __init__(self, tools: list[str]) -> None:
        listed = ", ".join(sorted(tools)[:5]) + (
            f" (+{len(tools) - 5} more)" if len(tools) > 5 else ""
        )
        super().__init__(
            status_code=400,
            message=(
                f"This API key may not use the following tools: {listed}. "
                "Remove them from the request or contact the operator to "
                "extend the key's allowed_tools."
            ),
            error_type="invalid_request_error",
            code="tool_not_allowed",
            param="tools",
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


class MCPDisabledError(GatewayError):
    """MCP proxy (tier 33) is off (``mcp.enabled: false``): the routes
    404 like every disabled surface (batch's precedent) — existence is
    not leaked to callers without the admin surface."""

    def __init__(self) -> None:
        super().__init__(
            status_code=404,
            message="The MCP proxy is disabled (config: mcp.enabled: false).",
            error_type="invalid_request_error",
            code="mcp_disabled",
        )


class MCPServerNotAllowedError(GatewayError):
    """MCP proxy (tier 33): the named server is either not configured
    or outside this key's (or its tenant's) ``allowed_mcp_servers``
    patterns. The ModelNotAllowedError shape deliberately — 404 with the
    same wording, so server existence is not leaked."""

    def __init__(self, server: str) -> None:
        super().__init__(
            status_code=404,
            message=f"The MCP server '{server}' does not exist or is not available for this API key.",
            error_type="invalid_request_error",
            code="mcp_server_not_found",
        )
