"""Loopback-only model transport. No SSH, proxies, redirects, or provider fallback."""

from dataclasses import dataclass
import http.client
import json
import math
import os
import re
from urllib.parse import urlsplit

from .analysis_context import AnalysisContext
from .model_client import (
    AnalysisOutputValidationError, ModelAvailability, ModelErrorCode,
    ModelResponse, ModelUnavailableError,
    FAILURE_KINDS, network_error,
)
from .request_trace import read_bounded, relay_headers, remaining_timeout


TOKEN_ENV = "SBATCH_AGENT_AI_RELAY_TOKEN"
REQUEST_LIMIT = 256 * 1024
RESPONSE_LIMIT = 132 * 1024


def relay_token():
    token = os.environ.get(TOKEN_ENV, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ModelUnavailableError(code=ModelErrorCode.CREDENTIAL_MISSING)
    return token


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def validate_request(value):
    """Exactly the existing ModelClient inputs, without local evidence metadata."""
    if (not isinstance(value, dict) or set(value) != {"system", "user", "schema"}
            or not all(isinstance(value[k], str) and value[k] for k in ("system", "user"))
            or not isinstance(value["schema"], dict) or value["schema"].get("type") != "object"):
        raise ValueError("Invalid relay request")
    context = value["system"] + value["user"]
    if (len(context) > 32000 or len(context.encode("utf-8")) > 96000
            or len(json_bytes(value["schema"])) > 64 * 1024
            or len(json_bytes(value)) > REQUEST_LIMIT):
        raise ValueError("Relay request exceeds limits")
    return value


def validate_response(value, protected=()):
    """Validate only the wire envelope; business schema/Harness stay on server."""
    try:
        if (not isinstance(value, dict) or set(value) != {"data", "request_id"}
                or not isinstance(value["data"], dict)):
            raise ValueError
        request_id = value["request_id"]
        if request_id is not None and (not isinstance(request_id, str)
                                      or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id)):
            raise ValueError
        raw = json_bytes(value)
        # Inspect decoded strings too: JSON escaping must not evade reflection
        # checks. This guards known exact secrets, not arbitrary encoded DLP.
        def strings(item):
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                for key, child in item.items():
                    yield key
                    yield from strings(child)
            elif isinstance(item, list):
                for child in item:
                    yield from strings(child)
        if len(raw) > RESPONSE_LIMIT or any(secret and secret in s for s in strings(value) for secret in protected):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AnalysisOutputValidationError("Relay returned invalid structured output.") from None
    return ModelResponse(value["data"], request_id)


@dataclass(frozen=True)
class RelayConfig:
    model: str
    endpoint: str
    timeout: float = 90.0

    def __post_init__(self):
        try:
            parsed = urlsplit(self.endpoint)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                    or parsed.netloc != f"127.0.0.1:{parsed.port}"
                    or not parsed.port or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                raise ValueError
            if (not self.model or not self.model.isprintable() or len(self.model) > 200
                    or isinstance(self.timeout, bool) or not math.isfinite(self.timeout)
                    or not 0 < self.timeout <= 120):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid loopback relay configuration") from None

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("SBATCH_AGENT_AI_MODEL", ""),
                   os.environ.get("SBATCH_AGENT_AI_RELAY_URL", ""),
                   float(os.environ.get("SBATCH_AGENT_AI_TIMEOUT", "90")))


class RemoteRelayModelClient:
    provider = "relay"

    def __init__(self, config: RelayConfig):
        self.config, self.model = config, config.model

    def availability(self):
        # Readiness only, same as the direct client; no implicit health/API call.
        try:
            relay_token()
        except ModelUnavailableError:
            return ModelAvailability("credential_missing", self.model)
        return ModelAvailability("available", self.model)

    def generate_structured(self, *, context: AnalysisContext, schema: dict) -> ModelResponse:
        token = relay_token()
        try:
            body = json_bytes(validate_request({"system": context.system, "user": context.user, "schema": schema}))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise AnalysisOutputValidationError("Relay request violates the bounded model contract.") from None
        # HTTPConnection uses the explicit loopback destination only. It does
        # not consult HTTP_PROXY, forward redirects, or alter any proxy setting.
        connection = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", urlsplit(self.config.endpoint).port,
                                                    timeout=remaining_timeout(self.config.timeout))
            connection.request("POST", "/v1/analyze", body=body, headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + token,
                **relay_headers(),
            })
            response = connection.getresponse()
            raw = read_bounded(response, RESPONSE_LIMIT, self.config.timeout, connection=connection)
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise network_error(exc, origin="relay") from None
        finally:
            if connection is not None:
                connection.close()
        if status != 200:
            raise relay_failure(status, raw)
        try:
            if len(raw) > RESPONSE_LIMIT:
                raise ValueError
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise AnalysisOutputValidationError("Relay returned invalid structured output.") from None
        return validate_response(value, (token,))


def relay_failure(status, raw):
    """Recognize our bounded protocol, NOT arbitrary HTTP 5xx from a listener."""
    error = None
    try:
        if len(raw) <= 1024:
            error = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        pass
    if status == 502 and error == {"error": "invalid_structured_response"}:
        return AnalysisOutputValidationError("Relay provider returned invalid structured output.")
    if status in {401, 403}:
        return ModelUnavailableError(code=ModelErrorCode.AUTHENTICATION, origin="relay", http_status=status)
    if status == 429 and error == {"error": "relay_busy"}:
        return ModelUnavailableError(code=ModelErrorCode.RATE_LIMIT, origin="relay", http_status=status)
    if isinstance(error, dict) and set(error) in ({"error"}, {"error", "http_status", "failure_kind"}):
        try:
            code = ModelErrorCode(error["error"])
            upstream_status = error.get("http_status")
            kind = error.get("failure_kind", "unknown")
            expected_status = 504 if code == ModelErrorCode.TIMEOUT else 429 if code == ModelErrorCode.RATE_LIMIT else 502
            if (status == expected_status and kind in FAILURE_KINDS
                    and (upstream_status is None or type(upstream_status) is int and 100 <= upstream_status <= 599)):
                return ModelUnavailableError(code=code, origin="provider", http_status=upstream_status, failure_kind=kind)
        except (ValueError, TypeError):
            pass
    return ModelUnavailableError(origin="relay", http_status=status, failure_kind="protocol")
