"""One error contract for the whole app: every failure gets a stable code and a human message."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any


class InfraScanError(Exception):
    """Raised at the API boundary; rendered as ``{"error": {code, message, detail}}``."""

    def __init__(self, code: str, message: str, *, status: int = 400, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


# ---------------------------------------------------------------- provider kinds


class ProviderError(InfraScanError):
    """A Meta Model API (or network) failure, already classified."""

    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 code: str = "", retryable: bool = False) -> None:
        super().__init__(f"provider_{kind}", message, status=_status_for(kind, status),
                         detail={"kind": kind, "provider_code": code, "retryable": retryable,
                                 "http_status": status})
        self.kind = kind
        self.provider_code = code
        self.retryable = retryable
        self.status_source = status


def _status_for(kind: str, status: int | None) -> int:
    if status:
        return status if 400 <= status < 600 else 502
    return {
        "timeout": 504,
        "unavailable": 502,
        "network": 502,
        "invalid_response": 502,
    }.get(kind, 500)


# Codes documented at https://dev.meta.ai/docs/error-handling : the envelope carries
# error.{message,type,param,code}; 401 invalid_api_key, 403 permission_error, 404 model_not_found.
def classify(status: int | None, body: str = "", provider_code: str = "") -> tuple[str, bool]:
    """Map an upstream response onto (kind, retryable)."""
    text = (body or "").lower()
    code = (provider_code or "").lower()
    if code in {"invalid_api_key", "authentication_error"} or "invalid api key" in text:
        return "authentication", False
    if status == 401:
        return "authentication", False
    if status == 403 or code == "permission_error":
        return "permission", False
    if status == 404 and (code == "model_not_found" or "model" in text):
        return "unknown_model", False
    if status == 404:
        return "invalid_request", False
    if status == 429 or code == "rate_limit_exceeded":
        return "rate_limited", True
    if status in (400, 413, 415, 422):
        return "invalid_request", False
    if status and status >= 500:
        return "unavailable", True
    return "unknown", False


def from_transport(exc: Exception) -> ProviderError:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", "the model request timed out; nothing was lost, retry when ready", retryable=True)
    if isinstance(exc, httpx.HTTPError):
        return ProviderError("network", f"network failure while contacting the model API: {exc}", retryable=True)
    return ProviderError("unknown", f"unexpected provider failure: {exc}", retryable=False)


# ------------------------------------------------------------------ normalisation

_KIND_HINTS = {
    "authentication": "Add MODEL_API_KEY to infrascan-3d/.env (never commit it) and restart the server.",
    "permission": "The key is valid but not entitled to this model or endpoint.",
    "unknown_model": "The configured model id is not offered by this API key. Check /api/providers.",
    "rate_limited": "Rate limit reached (3000 RPM / 4M TPM on the Spark line). Back off and retry.",
    "invalid_request": "The request was rejected by the provider; the prompt or media is at fault.",
    "timeout": "The provider did not answer in time.",
    "unavailable": "The provider returned a server-side error.",
    "network": "The provider could not be reached from this machine.",
    "invalid_response": "The provider answered, but the payload did not match the documented schema.",
}


def normalize(exc: Exception) -> InfraScanError:
    """Turn anything into the single error contract the frontend understands."""
    if isinstance(exc, ProviderError):
        hint = _KIND_HINTS.get(exc.kind)
        if hint:
            exc.detail["hint"] = hint
        return exc
    if isinstance(exc, InfraScanError):
        return exc
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return InfraScanError("invalid_input", str(exc) or "invalid input", status=422)
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    if isinstance(exc, OSError) and exc.filename:
        return InfraScanError("storage_error", f"storage problem: {exc.strerror}", status=500,
                              detail={"filename": str(exc.filename)})
    return InfraScanError("internal_error", f"{type(exc).__name__}: {exc}", status=500)


_KV = re.compile(r"(?i)(\"?'?[\w-]*(?:api[_-]?key|authorization|token|secret|key)\"?'?)([\"'\s]*[=:][\"'\s]*)[^\s,;\"'}]+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[^\s,;\"'}]+")
_BARE_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}")


def redact(text: str) -> str:
    """Never let a credential reach a log line, an error message or the audit trail.

    Only assignment-shaped values are masked, so an env var *name* in honest prose
    ("set MODEL_API_KEY in .env") survives while its value never can.
    """
    masked = _BEARER.sub(lambda m: f"{m.group(1)}[redacted]", text or "")
    masked = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", masked)
    return _BARE_KEY.sub("sk-[redacted]", masked)
