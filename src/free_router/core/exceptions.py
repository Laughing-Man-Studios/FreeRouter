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
    "NoAvailableCapacityError",
    "ProviderAuthError",
    "ProviderBaseError",
    "ProviderRateLimitError",
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


class NoAvailableCapacityError(ProviderBaseError):
    """No eligible credential or no remaining capacity; maps to HTTP 503.

    Distinct from :class:`ProviderServerError` because the two call for opposite
    responses. A 503 here is the router working correctly -- it refused to
    dispatch into an exhausted free tier rather than sending a request that
    could only fail -- and the correct client behaviour is to back off, not
    retry immediately. Reporting it as a provider fault would suggest a
    transient upstream problem and invite exactly the retry the cooldown exists
    to prevent (CONSTITUTION §6.1).

    ``503`` is new in M1 for that reason: "no capacity" is a real and expected
    outcome on free tiers, and it was previously indistinguishable from a fault.
    """

    error_type = "rate_limit_error"
    code = "no_available_capacity"


class ProviderAuthError(ProviderBaseError):
    """The provider rejected our credentials; maps to HTTP 401."""

    code = "provider_auth_error"


class ProviderValidationError(ProviderBaseError):
    """The provider rejected the payload we sent; maps to HTTP 400."""

    error_type = "invalid_request_error"
    code = "provider_validation_error"


class ProviderRateLimitError(ProviderBaseError):
    """The provider refused the request for rate-limit reasons; maps to HTTP 429.

    Distinct from :class:`ProviderValidationError` because the two demand
    opposite responses. A rate limit is a *capacity* signal: retrying the same
    request immediately makes it worse, and spec "Retry & Failover" forbids
    retrying into one. A validation error is a request the provider will never
    accept, so it must not be retried at all. Collapsing the two — as M0 did,
    mapping every 4xx to a validation error — reports a rate limit to the
    client as ``400 invalid_request_error``, which reads as "the client sent
    something wrong" and sends an operator to debug their payload.
    """

    error_type = "rate_limit_error"
    code = "provider_rate_limited"


class ProviderServerError(ProviderBaseError):
    """The provider returned 5xx; maps to HTTP 502."""

    code = "provider_server_error"


class ProviderTimeoutError(ProviderBaseError):
    """The provider did not respond inside the routing envelope; maps to HTTP 504."""

    error_type = "timeout_error"
    code = "provider_timeout"
