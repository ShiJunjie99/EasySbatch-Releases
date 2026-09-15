"""Bounded structured RPC shared by the server and current-user Launcher."""

from __future__ import annotations

from enum import StrEnum
import json
import math
import re
from uuid import UUID


PROVIDER = "deepseek"
MODEL = "deepseek-chat"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_PROVIDER_REQUEST_BYTES = 256 * 1024
MAX_PROVIDER_RESPONSE_BODY_BYTES = 128 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 160 * 1024
MAX_PROVIDER_STATUS_BYTES = 1024
MAX_PROVIDER_TIMEOUT_SECONDS = 120.0
ALLOWED_BACKENDS = frozenset({
    "Windows Credential Manager", "macOS Keychain", "Secret Service",
    "KWallet", "Session only", "Unavailable",
})


class LocalAIErrorCode(StrEnum):
    NOT_CONFIGURED = "AI_NOT_CONFIGURED"
    CREDENTIAL_UNAVAILABLE = "AI_LOCAL_CREDENTIAL_UNAVAILABLE"
    AUTH_FAILED = "AI_PROVIDER_AUTH_FAILED"
    UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
    RATE_LIMITED = "AI_PROVIDER_RATE_LIMITED"
    TIMEOUT = "AI_PROVIDER_TIMEOUT"
    RESPONSE_INVALID = "AI_PROVIDER_RESPONSE_INVALID"
    IDENTITY_MISMATCH = "AI_PROVIDER_IDENTITY_MISMATCH"
    CLIENT_DISCONNECTED = "AI_CLIENT_DISCONNECTED"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError from None
    return value


def _timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or not 0 < value <= MAX_PROVIDER_TIMEOUT_SECONDS):
        raise ValueError
    return float(value)


def validate_provider_request(value):
    if not isinstance(value, dict) or set(value) != {
            "request_id", "provider", "model", "messages", "parameters",
            "timeout_seconds", "metadata"}:
        raise ValueError("AI_PROVIDER_REQUEST_INVALID")
    _uuid(value["request_id"])
    if value["provider"] != PROVIDER or value["model"] != MODEL:
        raise ValueError("AI_PROVIDER_REQUEST_INVALID")
    messages = value["messages"]
    if (not isinstance(messages, list) or len(messages) != 2 or
            [item.get("role") if isinstance(item, dict) else None for item in messages]
            != ["system", "user"]):
        raise ValueError("AI_PROVIDER_REQUEST_INVALID")
    for item in messages:
        if (set(item) != {"role", "content"} or
                not isinstance(item["content"], str) or not item["content"] or
                len(item["content"]) > 128000):
            raise ValueError("AI_PROVIDER_REQUEST_INVALID")
    combined = "".join(item["content"] for item in messages)
    # M6 bounds the prompt/context before the server appends its own JSON
    # schema.  The final RPC remains separately bounded here.
    if len(combined) > 160000 or len(combined.encode("utf-8")) > 224000:
        raise ValueError("AI_PROVIDER_REQUEST_INVALID")
    parameters = value["parameters"]
    if (not isinstance(parameters, dict) or set(parameters) != {
            "response_format", "max_tokens", "thinking", "temperature", "stream"} or
            parameters["response_format"] != {"type": "json_object"} or
            parameters["thinking"] != {"type": "disabled"} or
            type(parameters["max_tokens"]) is not int or
            not 1 <= parameters["max_tokens"] <= 8192 or
            parameters["temperature"] != 0 or parameters["stream"] is not False):
        raise ValueError("AI_PROVIDER_REQUEST_INVALID")
    _timeout(value["timeout_seconds"])
    metadata = value["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {"prepare_request_id"}:
        raise ValueError("AI_PROVIDER_REQUEST_INVALID")
    correlation = metadata["prepare_request_id"]
    if correlation is not None:
        _uuid(correlation)
    if len(json_bytes(value)) > MAX_PROVIDER_REQUEST_BYTES:
        raise ValueError("AI_PROVIDER_REQUEST_TOO_LARGE")
    return value


def encode_provider_request(value):
    validate_provider_request(value)
    return json_bytes(value)


def decode_provider_request(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PROVIDER_REQUEST_BYTES:
        raise ValueError("AI_PROVIDER_REQUEST_TOO_LARGE")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return validate_provider_request(value)
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError):
        raise ValueError("AI_PROVIDER_REQUEST_INVALID") from None


def provider_http_body(request):
    """Create the fixed DeepSeek Chat Completions body; no URL is accepted."""
    validate_provider_request(request)
    return {
        "model": request["model"], "messages": request["messages"],
        **request["parameters"],
    }


def _usage(value):
    if not isinstance(value, dict):
        return {"input_tokens": None, "output_tokens": None}
    input_tokens = value.get("prompt_tokens")
    output_tokens = value.get("completion_tokens")
    return {
        "input_tokens": input_tokens if type(input_tokens) is int and input_tokens >= 0 else None,
        "output_tokens": output_tokens if type(output_tokens) is int and output_tokens >= 0 else None,
    }


def success_response(request_id, provider_response):
    _uuid(request_id)
    if not isinstance(provider_response, dict):
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    value = {
        "request_id": request_id, "status": "ok", "provider": PROVIDER,
        "provider_response": provider_response,
        "usage": _usage(provider_response.get("usage")), "error_category": None,
        "http_status": 200,
    }
    if len(json_bytes(value)) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ValueError("AI_PROVIDER_RESPONSE_TOO_LARGE")
    return value


def error_response(request_id, category, *, http_status=None):
    _uuid(request_id)
    category = LocalAIErrorCode(category)
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    return {
        "request_id": request_id, "status": "error", "provider": PROVIDER,
        "provider_response": None, "usage": {"input_tokens": None, "output_tokens": None},
        "error_category": category.value, "http_status": http_status,
    }


def validate_provider_response(value, *, request_id=None):
    if not isinstance(value, dict) or set(value) != {
            "request_id", "status", "provider", "provider_response", "usage",
            "error_category", "http_status"}:
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    _uuid(value["request_id"])
    if request_id is not None and value["request_id"] != request_id:
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    if value["provider"] != PROVIDER or value["status"] not in {"ok", "error"}:
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    usage = value["usage"]
    if (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"} or
            any(item is not None and (type(item) is not int or item < 0)
                for item in usage.values())):
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    http_status = value["http_status"]
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    if value["status"] == "ok":
        if (not isinstance(value["provider_response"], dict) or
                value["error_category"] is not None or http_status != 200):
            raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
    else:
        if value["provider_response"] is not None:
            raise ValueError("AI_PROVIDER_RESPONSE_INVALID")
        LocalAIErrorCode(value["error_category"])
    if len(json_bytes(value)) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ValueError("AI_PROVIDER_RESPONSE_TOO_LARGE")
    return value


def encode_provider_response(value):
    validate_provider_response(value)
    return json_bytes(value)


def decode_provider_response(raw, *, request_id=None):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ValueError("AI_PROVIDER_RESPONSE_TOO_LARGE")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return validate_provider_response(value, request_id=request_id)
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError):
        raise ValueError("AI_PROVIDER_RESPONSE_INVALID") from None


def provider_status(*, provider=PROVIDER, configured, backend, availability=None):
    if provider != PROVIDER or type(configured) is not bool or backend not in ALLOWED_BACKENDS:
        raise ValueError("AI_PROVIDER_STATUS_INVALID")
    availability = availability or ("configured" if configured else
                                    "unavailable" if backend == "Unavailable" else "not_configured")
    if availability not in {"configured", "not_configured", "unavailable"}:
        raise ValueError("AI_PROVIDER_STATUS_INVALID")
    return {"provider": PROVIDER, "configured": configured,
            "backend": backend, "availability": availability}


def encode_provider_status(value):
    if not isinstance(value, dict):
        raise ValueError("AI_PROVIDER_STATUS_INVALID")
    validated = provider_status(**value)
    raw = json_bytes(validated)
    if len(raw) > MAX_PROVIDER_STATUS_BYTES:
        raise ValueError("AI_PROVIDER_STATUS_INVALID")
    return raw


def decode_provider_status(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PROVIDER_STATUS_BYTES:
        raise ValueError("AI_PROVIDER_STATUS_INVALID")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {
                "provider", "configured", "backend", "availability"}:
            raise ValueError
        return provider_status(**value)
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError):
        raise ValueError("AI_PROVIDER_STATUS_INVALID") from None
