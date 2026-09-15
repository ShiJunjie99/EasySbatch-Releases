"""Server-side ModelClient for one exact current-user Launcher session."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import re
from uuid import uuid4

from .local_ai_protocol import (
    DEEPSEEK_ENDPOINT, LocalAIErrorCode, MODEL, PROVIDER,
    validate_provider_response,
)
from .model_client import (
    ModelErrorCode, ModelResponse, ModelUnavailableError,
)
from .request_trace import current_trace, remaining_timeout


@dataclass(frozen=True)
class LocalUserProviderConfig:
    provider: str = PROVIDER
    model: str = MODEL
    endpoint: str = DEEPSEEK_ENDPOINT
    timeout: float = 60.0

    def __post_init__(self):
        if (self.provider != PROVIDER or self.model != MODEL or
                self.endpoint != DEEPSEEK_ENDPOINT or
                isinstance(self.timeout, bool) or
                not isinstance(self.timeout, (int, float)) or
                not math.isfinite(self.timeout) or not 0 < self.timeout <= 120):
            raise ValueError("Invalid local-user AI provider configuration")

    @classmethod
    def from_env(cls):
        provider = os.environ.get("SBATCH_AGENT_AI_PROVIDER") or PROVIDER
        model = os.environ.get("SBATCH_AGENT_AI_MODEL") or MODEL
        endpoint = os.environ.get("SBATCH_AGENT_AI_ENDPOINT") or DEEPSEEK_ENDPOINT
        timeout = float(os.environ.get("SBATCH_AGENT_AI_TIMEOUT", "60"))
        return cls(provider=provider, model=model, endpoint=endpoint, timeout=timeout)


def _invalid_response():
    return ModelUnavailableError(
        code=ModelErrorCode.PROVIDER_RESPONSE_INVALID,
        origin="local_provider", failure_kind="protocol",
    )


def _parse_provider_response(payload):
    try:
        choices = payload["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise ValueError
        message = choice["message"]
        if (not isinstance(message, dict) or message.get("refusal") or
                message.get("tool_calls") or message.get("function_call") or
                not isinstance(message.get("content"), str)):
            raise ValueError
        data = json.loads(message["content"])
        if not isinstance(data, dict):
            raise ValueError
        request_id = payload.get("id")
        if request_id is not None and (
                not isinstance(request_id, str) or
                re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id) is None):
            request_id = None
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        input_tokens = input_tokens if type(input_tokens) is int and input_tokens >= 0 else None
        output_tokens = output_tokens if type(output_tokens) is int and output_tokens >= 0 else None
        return ModelResponse(data, request_id, input_tokens, output_tokens)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError,
            json.JSONDecodeError, RecursionError):
        raise _invalid_response() from None


class LocalUserProviderModelClient:
    """Prompt builder/parser on the server; network and credential stay local."""

    def __init__(self, config, *, session, audit_session_id=None, username=None):
        if not isinstance(config, LocalUserProviderConfig):
            raise ValueError("LocalUserProviderConfig required")
        self.config = config
        self.session = session
        self.audit_session_id = audit_session_id
        self.username = username
        self.provider, self.model = config.provider, config.model

    def __repr__(self):
        return (f"LocalUserProviderModelClient(provider={self.provider!r}, "
                f"model={self.model!r}, username={self.username!r})")

    def availability(self):
        from .model_client import ModelAvailability
        return ModelAvailability("available" if self.session.available else
                                 "credential_missing", self.model)

    @staticmethod
    def _provider_error(response):
        category = LocalAIErrorCode(response["error_category"])
        code = {
            LocalAIErrorCode.NOT_CONFIGURED: ModelErrorCode.LOCAL_NOT_CONFIGURED,
            LocalAIErrorCode.CREDENTIAL_UNAVAILABLE: ModelErrorCode.LOCAL_CREDENTIAL_UNAVAILABLE,
            LocalAIErrorCode.AUTH_FAILED: ModelErrorCode.AUTHENTICATION,
            LocalAIErrorCode.UNAVAILABLE: ModelErrorCode.UNAVAILABLE,
            LocalAIErrorCode.RATE_LIMITED: ModelErrorCode.RATE_LIMIT,
            LocalAIErrorCode.TIMEOUT: ModelErrorCode.TIMEOUT,
            LocalAIErrorCode.RESPONSE_INVALID: ModelErrorCode.PROVIDER_RESPONSE_INVALID,
            LocalAIErrorCode.IDENTITY_MISMATCH: ModelErrorCode.LOCAL_IDENTITY_MISMATCH,
            LocalAIErrorCode.CLIENT_DISCONNECTED: ModelErrorCode.LOCAL_CLIENT_DISCONNECTED,
        }[category]
        raise ModelUnavailableError(
            code=code, http_status=response.get("http_status"),
            origin="local_provider",
            failure_kind=("timeout" if category == LocalAIErrorCode.TIMEOUT else
                          "http" if response.get("http_status") else
                          "protocol" if category in {
                              LocalAIErrorCode.RESPONSE_INVALID,
                              LocalAIErrorCode.IDENTITY_MISMATCH,
                              LocalAIErrorCode.CLIENT_DISCONNECTED,
                          } else "unknown"),
        )

    def generate_structured(self, *, context, schema):
        system = context.system + (
            "\nReturn one JSON object conforming to this JSON Schema. "
            "Do not use Markdown. Use null/UNRESOLVED for unsupported fields.\n"
            "OUTPUT JSON SCHEMA:\n" + json.dumps(schema, ensure_ascii=False)
        )
        request_id = str(uuid4())
        trace = current_trace()
        try:
            request_timeout = remaining_timeout(self.config.timeout)
        except TimeoutError:
            raise ModelUnavailableError(
                code=ModelErrorCode.TIMEOUT, origin="local_provider",
                failure_kind="overall_timeout",
            ) from None
        request = {
            "request_id": request_id, "provider": self.provider, "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": context.user},
            ],
            "parameters": {
                "response_format": {"type": "json_object"}, "max_tokens": 8192,
                "thinking": {"type": "disabled"}, "temperature": 0,
                "stream": False,
            },
            "timeout_seconds": request_timeout,
            "metadata": {"prepare_request_id": trace.request_id if trace else None},
        }
        try:
            response = self.session.request_provider(
                request, timeout=request_timeout,
            )
            validate_provider_response(response, request_id=request_id)
        except ModelUnavailableError:
            raise
        except Exception as exc:
            category = getattr(exc, "code", "")
            code = {
                "AI_PROVIDER_IDENTITY_MISMATCH": ModelErrorCode.LOCAL_IDENTITY_MISMATCH,
                "WORKER_IDENTITY_MISMATCH": ModelErrorCode.LOCAL_IDENTITY_MISMATCH,
                "AI_PROVIDER_TIMEOUT": ModelErrorCode.TIMEOUT,
                "AI_PROVIDER_RATE_LIMITED": ModelErrorCode.RATE_LIMIT,
                "AI_PROVIDER_RESPONSE_INVALID": ModelErrorCode.PROVIDER_RESPONSE_INVALID,
                "AI_CLIENT_DISCONNECTED": ModelErrorCode.LOCAL_CLIENT_DISCONNECTED,
            }.get(category, ModelErrorCode.LOCAL_CLIENT_DISCONNECTED)
            failure_kind = ("timeout" if code == ModelErrorCode.TIMEOUT else
                            "http" if code == ModelErrorCode.RATE_LIMIT else "protocol")
            raise ModelUnavailableError(
                code=code, origin="local_provider", failure_kind=failure_kind,
            ) from None
        if response["status"] != "ok":
            self._provider_error(response)
        return _parse_provider_response(response["provider_response"])
