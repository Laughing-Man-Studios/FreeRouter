"""Router exception hierarchy.

Every failure the router surfaces to a client is one of these exceptions. The
mapping from exception to HTTP status code and OpenAI-compatible error body
lives in :mod:`free_router.api.main`, so provider-specific ``httpx`` handling
stays inside the adapters and response formatting stays in one place
(CONSTITUTION 6.1, spec "Error Behavior").

Each exception carries the ``error_type``, ``code``, and ``param`` that the
OpenAI error envelope requires, so a handler never has to guess them.
"""

from __future__ import annotations

__all__ = [
    "ProviderAuthError",
    "ProviderBaseError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "ProviderValidationError",
    "RouterBaseError",
    "UnsupportedModelError",
    "ValidationError",
]


class RouterBaseError(Exception):
    """Base class for every error the router reports to a client.

    Attributes:
        message: Human-readable description. Must never embed prompt text,
            completion text, or secret values.
        error_type: The OpenAI ``error.type`` value, e.g. ``invalid_request_error``.
        code: A stable machine-readable identifier, e.g. ``unsupported_model``.
        param: The offending request field, or ``None``.
    """

    error_type: str = "api_error"
    code: str = "router_error"
    param: str | None = None

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if error_type is not None:
            self.error_type = error_type
        if code is not None:
            self.code = code
        if param is not None:
            self.param = param

    def to_payload(self) -> dict[str, object]:
        """Render the OpenAI-compatible error envelope for this error."""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


class ValidationError(RouterBaseError):
    """The client's request is malformed or violates a router rule.

    Raised by the ingress layer before any outbound provider call, e.g. an
    empty ``messages`` array (spec Scenario 6).
    """

    error_type = "invalid_request_error"
    code = "invalid_request"


class UnsupportedModelError(ValidationError):
    """The requested model is not mapped in configuration (spec Scenario 7).

    Raised before dispatch: the router must never substitute a different model
    for the one the client asked for (spec "No Automatic Fallback").
    """

    code = "unsupported_model"


class ProviderBaseError(RouterBaseError):
    """Base class for a failure originating at an upstream provider."""

    error_type = "api_error"
    code = "provider_error"


class ProviderAuthError(ProviderBaseError):
    """The provider rejected our credentials; maps to HTTP 401."""

    code = "provider_auth_error"


class ProviderValidationError(ProviderBaseError):
    """The provider rejected the payload we sent; maps to HTTP 400."""

    error_type = "invalid_request_error"
    code = "provider_validation_error"


class ProviderServerError(ProviderBaseError):
    """The provider returned 5xx; maps to HTTP 502."""

    code = "provider_server_error"


class ProviderTimeoutError(ProviderBaseError):
    """The provider did not respond inside the routing envelope; maps to HTTP 504."""

    error_type = "timeout_error"
    code = "provider_timeout"
