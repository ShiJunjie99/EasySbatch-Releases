"""One structured inference request. No agent tools, retries or default provider."""

from dataclasses import dataclass
from enum import Enum
import json
import math
import os
import re
import ssl
import socket
import http.client
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .ai_egress import AIEgressError, AIEgressErrorCode
from .analysis_context import AnalysisContext
from .request_trace import read_bounded, remaining_timeout


class ModelErrorCode(str, Enum):
    NOT_CONFIGURED = "provider_not_configured"
    INVALID_CONFIG = "invalid_configuration"
    CREDENTIAL_MISSING = "credential_missing"
    AUTHENTICATION = "authentication_failure"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit_or_quota"
    TLS_CERTIFICATE = "tls_certificate_error"
    UNAVAILABLE = "provider_unavailable"
    EGRESS_UNAVAILABLE = "ai_egress_unavailable"
    EGRESS_AUTHENTICATION = "ai_egress_authentication_failure"
    EGRESS_IDENTITY = "ai_egress_identity_mismatch"
    EGRESS_TARGET = "ai_egress_target_rejected"
    EGRESS_TLS = "ai_egress_tls_failure"
    EGRESS_TIMEOUT = "ai_egress_timeout"
    LOCAL_CREDENTIAL_UNAVAILABLE = "local_credential_unavailable"
    LOCAL_NOT_CONFIGURED = "local_ai_not_configured"
    PROVIDER_RESPONSE_INVALID = "provider_response_invalid"
    LOCAL_IDENTITY_MISMATCH = "local_provider_identity_mismatch"
    LOCAL_CLIENT_DISCONNECTED = "local_ai_client_disconnected"


MODEL_ERROR_MESSAGES = {
    ModelErrorCode.NOT_CONFIGURED: "AI provider is not configured.",
    ModelErrorCode.INVALID_CONFIG: "AI provider configuration is incomplete or invalid.",
    ModelErrorCode.CREDENTIAL_MISSING: "AI credential is missing or invalid in the backend environment.",
    ModelErrorCode.AUTHENTICATION: "AI authentication or access was rejected; check backend credentials and permissions.",
    ModelErrorCode.TIMEOUT: "AI provider request timed out; analysis uses a bounded retry budget.",
    ModelErrorCode.RATE_LIMIT: "AI provider rate limit or quota reached; analysis uses a bounded retry budget.",
    ModelErrorCode.TLS_CERTIFICATE: "AI HTTPS certificate verification failed. 请检查后端 CA 证书配置（ca_bundle / SSL_CERT_FILE）；手工模式仍可使用。",
    ModelErrorCode.UNAVAILABLE: "AI provider is currently unavailable; manual mode remains available.",
    ModelErrorCode.EGRESS_UNAVAILABLE: "This session's AI network egress is unavailable; manual mode remains available.",
    ModelErrorCode.EGRESS_AUTHENTICATION: "This session's AI network egress rejected authentication.",
    ModelErrorCode.EGRESS_IDENTITY: "The AI network egress does not belong to this session.",
    ModelErrorCode.EGRESS_TARGET: "The AI network egress rejected the requested destination.",
    ModelErrorCode.EGRESS_TLS: "The AI network egress could not establish a verified TLS connection.",
    ModelErrorCode.EGRESS_TIMEOUT: "This session's AI network egress timed out.",
    ModelErrorCode.LOCAL_CREDENTIAL_UNAVAILABLE: "The local secure credential store is unavailable.",
    ModelErrorCode.LOCAL_NOT_CONFIGURED: "DeepSeek is not configured for this local user.",
    ModelErrorCode.PROVIDER_RESPONSE_INVALID: "The local AI provider returned an invalid response.",
    ModelErrorCode.LOCAL_IDENTITY_MISMATCH: "The local AI provider does not belong to this session.",
    ModelErrorCode.LOCAL_CLIENT_DISCONNECTED: "The current user's local AI client is disconnected.",
}


class ModelUnavailableError(RuntimeError):
    """Safe summary; never embeds provider bodies, credentials or prompts."""

    def __init__(self, message=None, *, code=ModelErrorCode.UNAVAILABLE, http_status=None,
                 origin="provider", failure_kind="unknown"):
        self.code = ModelErrorCode(code)
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.origin = origin if origin in {"provider", "relay", "egress", "local_provider"} else "provider"
        self.failure_kind = failure_kind if failure_kind in FAILURE_KINDS else "unknown"
        super().__init__(message or MODEL_ERROR_MESSAGES[self.code])


FAILURE_KINDS = {"unknown", "http", "timeout", "connect_timeout", "read_timeout", "overall_timeout",
                 "connection_refused", "connection_reset", "temporary_dns", "dns", "tls", "protocol"}


def network_error(exc, *, origin="provider"):
    """Classify OS types/errno only, never their potentially sensitive text."""
    if isinstance(exc, URLError):
        exc = exc.reason
    code, kind = ModelErrorCode.UNAVAILABLE, "unknown"
    if isinstance(exc, ssl.SSLError):
        code, kind = ModelErrorCode.TLS_CERTIFICATE, "tls"
    elif isinstance(exc, TimeoutError):
        code, kind = ModelErrorCode.TIMEOUT, "timeout"
    elif isinstance(exc, ConnectionRefusedError):
        kind = "connection_refused"
    elif isinstance(exc, (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
        kind = "connection_reset"
    elif isinstance(exc, socket.gaierror):
        kind = "temporary_dns" if exc.errno == socket.EAI_AGAIN else "dns"
    elif isinstance(exc, http.client.HTTPException):
        kind = "protocol"
    return ModelUnavailableError(code=code, origin=origin, failure_kind=kind)


@dataclass(frozen=True)
class ModelAvailability:
    """Configuration readiness only. Contains no config object, endpoint or key."""
    state: Literal["available", "unavailable", "not_configured", "invalid_configuration", "credential_missing"]
    model: str | None = None


class AnalysisOutputValidationError(ValueError):
    """Schema or semantic validation rejected the model output."""

    # Diagnostics are fixed vocabulary, never provider text, values or refs.
    STAGES = {"response", "schema", "evidence"}
    FIELDS = {"output", "draft", "conflicts", "notes", "model_metadata", "run_type", "work_dir",
              "entrypoint", "run_step.executable", "run_step.args", "required_inputs",
              "environment_requirements", "build", "parallelism.serial", "parallelism.threads",
              "parallelism.mpi", "parallelism.gpu", "resource_requirements.nodes",
              "resource_requirements.ntasks", "resource_requirements.cpus_per_task",
              "resource_requirements.gpu_count", "resource_requirements.memory_mib",
              "resource_requirements.time_limit_seconds"}
    REASONS = {"invalid_output", "truncated_output", "schema_mismatch", "inconsistent_proposal",
               "unknown_evidence", "unobserved_path", "unsupported_value"}

    def __init__(self, message, *, stage="response", field="output", reason="invalid_output"):
        super().__init__(message)
        self.stage = stage if stage in self.STAGES else "response"
        self.field = field if field in self.FIELDS else "output"
        self.reason = reason if reason in self.REASONS else "invalid_output"

    def safe_diagnostic(self):
        # Validate again at presentation, including custom/injected clients.
        stage = self.stage if self.stage in self.STAGES else "response"
        field = self.field if self.field in self.FIELDS else "output"
        reason = self.reason if self.reason in self.REASONS else "invalid_output"
        return f"stage={stage} field={field} reason={reason}"


@dataclass(frozen=True)
class ModelResponse:
    data: dict
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelClient(Protocol):
    provider: str
    model: str

    def generate_structured(self, *, context: AnalysisContext, schema: dict) -> ModelResponse: ...


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    endpoint: str
    api_key_env: str
    timeout: float = 30.0
    max_response_bytes: int = 128 * 1024

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if self.provider not in {"openai-compatible", "deepseek"}:
            raise ValueError("Unsupported AI provider; use openai-compatible or deepseek")
        if not self.model or not self.model.isprintable() or len(self.model) > 200:
            raise ValueError("AI model must be explicitly configured")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not self.endpoint.isprintable()):
            raise ValueError("AI endpoint must be an explicit HTTPS URL without credentials/query/fragment")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("Configure the credential environment variable name, never its value")
        if isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 120:
            raise ValueError("AI timeout must be in (0, 120] seconds")
        if type(self.max_response_bytes) is not int or not 0 < self.max_response_bytes <= 1024 * 1024:
            raise ValueError("Invalid AI response size limit")

    @classmethod
    def from_env(cls):
        # Do not probe other apps' credentials, dotenv files or login sessions.
        values = [os.environ.get("SBATCH_AGENT_AI_" + key, "") for key in
                  ("PROVIDER", "MODEL", "ENDPOINT", "API_KEY_ENV")]
        if not any(values):
            return None
        if not all(values):
            raise ValueError("AI provider/model/endpoint/API_KEY_ENV configuration is incomplete")
        return cls(*values, timeout=float(os.environ.get("SBATCH_AGENT_AI_TIMEOUT", "30")))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer credential to a redirect target.
        return None


class OpenAICompatibleClient:
    def __init__(self, config: ModelConfig, *, transport=None):
        self.config = config
        self.transport = transport
        self.provider, self.model = config.provider, config.model

    def _credential(self):
        key = os.environ.get(self.config.api_key_env, "")
        # Reject blanks and documentation placeholders without an API request.
        if (not key or not key.isascii() or not key.isprintable() or key.strip() != key
                or (key.startswith("<") and key.endswith(">"))):
            raise ModelUnavailableError(code=ModelErrorCode.CREDENTIAL_MISSING)
        return key

    def availability(self) -> ModelAvailability:
        """No network probe: credentials present does not prove authentication."""
        try:
            self._credential()
        except ModelUnavailableError:
            return ModelAvailability("credential_missing", self.model)
        if self.transport is not None and not getattr(self.transport, "available", True):
            return ModelAvailability("unavailable", self.model)
        return ModelAvailability("available", self.model)

    @staticmethod
    def _raise_http_error(code):
        category = (ModelErrorCode.AUTHENTICATION if code in {401, 403} else
                    ModelErrorCode.RATE_LIMIT if code == 429 else
                    ModelErrorCode.TIMEOUT if code in {408, 504} else
                    ModelErrorCode.INVALID_CONFIG if code in {400, 404, 422} else
                    ModelErrorCode.UNAVAILABLE)
        raise ModelUnavailableError(code=category, http_status=code, failure_kind="http")

    @staticmethod
    def _egress_error(exc):
        category = {
            AIEgressErrorCode.UNAVAILABLE: ModelErrorCode.EGRESS_UNAVAILABLE,
            AIEgressErrorCode.AUTH_FAILED: ModelErrorCode.EGRESS_AUTHENTICATION,
            AIEgressErrorCode.IDENTITY_MISMATCH: ModelErrorCode.EGRESS_IDENTITY,
            AIEgressErrorCode.TARGET_REJECTED: ModelErrorCode.EGRESS_TARGET,
            AIEgressErrorCode.TLS_FAILED: ModelErrorCode.EGRESS_TLS,
            AIEgressErrorCode.TIMEOUT: ModelErrorCode.EGRESS_TIMEOUT,
        }.get(exc.code, ModelErrorCode.EGRESS_UNAVAILABLE)
        kind = ("timeout" if exc.code == AIEgressErrorCode.TIMEOUT else
                "tls" if exc.code == AIEgressErrorCode.TLS_FAILED else "protocol")
        return ModelUnavailableError(code=category, origin="egress", failure_kind=kind)

    def generate_structured(self, *, context: AnalysisContext, schema: dict) -> ModelResponse:
        key = self._credential()
        body = self._request_body(context, schema)
        # No tools, tool_choice, code execution, or text-to-Shell fallback.
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        try:
            if self.transport is None:
                request = Request(self.config.endpoint, data=encoded, headers=headers, method="POST")
                with build_opener(_NoRedirect()).open(
                        request, timeout=remaining_timeout(self.config.timeout)) as response:
                    raw = read_bounded(response, self.config.max_response_bytes, self.config.timeout)
            else:
                status, raw = self.transport.request(
                    endpoint=self.config.endpoint, body=encoded, headers=headers,
                    timeout=self.config.timeout,
                    max_response_bytes=self.config.max_response_bytes,
                )
                if status < 200 or status >= 300:
                    self._raise_http_error(status)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            self._raise_http_error(code)
        except AIEgressError as exc:
            raise self._egress_error(exc) from None
        except (OSError, http.client.HTTPException) as exc:
            raise network_error(exc) from None
        if len(raw) > self.config.max_response_bytes:
            raise AnalysisOutputValidationError("AI response exceeds the configured size limit.")
        if key.encode("ascii") in raw:
            # A misbehaving proxy must not reflect the credential into draft/UI/logs.
            raise AnalysisOutputValidationError("AI response contains protected authentication data.")
        try:
            payload = json.loads(raw)
            choices = payload["choices"]
            if len(choices) == 1 and choices[0].get("finish_reason") == "length":
                raise AnalysisOutputValidationError("AI structured output was truncated.", reason="truncated_output")
            if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
                raise ValueError
            message = choices[0]["message"]
            if message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
                raise ValueError
            content = message["content"]
            if not isinstance(content, str):
                raise ValueError
            data = json.loads(content)  # JSON only: never strip fences or regex-repair.
            if not isinstance(data, dict):
                raise ValueError
            request_id = payload.get("id")
            if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id)):
                request_id = None
            usage = payload.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
            input_tokens = input_tokens if type(input_tokens) is int and input_tokens >= 0 else None
            output_tokens = output_tokens if type(output_tokens) is int and output_tokens >= 0 else None
        except AnalysisOutputValidationError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            raise AnalysisOutputValidationError("AI response is invalid, incomplete, or refused structured output.") from None
        return ModelResponse(data, request_id, input_tokens, output_tokens)

    def _request_body(self, context: AnalysisContext, schema: dict) -> dict:
        system = context.system
        if self.provider == "deepseek":
            # DeepSeek Chat Completions supports JSON Output, not strict
            # response_format.json_schema. Send the same schema as instructions;
            # AIProjectAnalyzer still applies StructuredAnalysis + post-validation.
            # JSON validity is not a guarantee of schema or semantic correctness.
            system += ("\nReturn one JSON object conforming to this JSON Schema. "
                       "Do not use Markdown. Use null/UNRESOLVED for unsupported fields.\n"
                       "OUTPUT JSON SCHEMA:\n" + json.dumps(schema, ensure_ascii=False))
            options = {"response_format": {"type": "json_object"}, "max_tokens": 8192,
                       "thinking": {"type": "disabled"}, "temperature": 0}
        else:
            options = {"response_format": {"type": "json_schema", "json_schema": {
                "name": "project_analysis", "strict": True, "schema": schema,
            }}, "store": False, "n": 1, "max_completion_tokens": 4096}
        return {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": context.user},
        ], "stream": False, **options}
