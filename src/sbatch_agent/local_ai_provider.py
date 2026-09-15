"""Launcher-side DeepSeek HTTPS client using only the local OS credential."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import uuid4

from .credential_store import (
    AICredentialManager, CredentialBackend, CredentialStoreError,
    create_ai_credential_manager,
)
from .local_ai_protocol import (
    DEEPSEEK_ENDPOINT, LocalAIErrorCode, MAX_PROVIDER_RESPONSE_BODY_BYTES,
    MODEL, PROVIDER, error_response, provider_http_body, provider_status,
    success_response, validate_provider_request,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verified_ssl_context():
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        context = ssl.create_default_context()
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise CredentialStoreError("AI_TLS_CONFIGURATION_INVALID")
    return context


def _read_bounded(response, limit):
    data = bytearray()
    while True:
        chunk = response.read(min(64 * 1024, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("AI_PROVIDER_RESPONSE_TOO_LARGE")


def _contains_secret(value, secret):
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains_secret(key, secret) or _contains_secret(child, secret)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_secret(child, secret) for child in value)
    return False


class LocalDeepSeekProviderClient:
    """Fixed provider/endpoint; prompt construction and Harness remain remote."""

    provider = PROVIDER
    model = MODEL

    def __init__(self, *, credentials=None, opener_factory=None,
                 ssl_context_factory=verified_ssl_context):
        self.credentials = credentials or create_ai_credential_manager()
        self._opener_factory = opener_factory
        self._ssl_context_factory = ssl_context_factory

    def __repr__(self):
        return (f"LocalDeepSeekProviderClient(provider={self.provider!r}, "
                f"model={self.model!r}, backend={self.credentials.backend.value!r}, "
                f"configured={self.credentials.exists()!r})")

    def status(self):
        configured = self.credentials.exists()
        backend = self.credentials.backend.value
        availability = ("configured" if configured else
                        "unavailable" if backend == CredentialBackend.UNAVAILABLE.value
                        else "not_configured")
        return provider_status(configured=configured, backend=backend,
                               availability=availability)

    @staticmethod
    def _http_error(request_id, status):
        category = (LocalAIErrorCode.AUTH_FAILED if status in {401, 403} else
                    LocalAIErrorCode.RATE_LIMITED if status == 429 else
                    LocalAIErrorCode.TIMEOUT if status in {408, 504} else
                    LocalAIErrorCode.UNAVAILABLE)
        return error_response(request_id, category, http_status=status)

    def _opener(self):
        context = self._ssl_context_factory()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise CredentialStoreError("AI_TLS_CONFIGURATION_INVALID")
        if self._opener_factory is not None:
            return self._opener_factory(context)
        return build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def request(self, value):
        """Return only a validated safe envelope; never raise provider text."""
        try:
            request_value = validate_provider_request(value)
            request_id = request_value["request_id"]
        except (ValueError, TypeError, RecursionError):
            request_id = str(uuid4())
            return error_response(request_id, LocalAIErrorCode.RESPONSE_INVALID)
        try:
            secret_value = self.credentials.get()
        except CredentialStoreError:
            return error_response(request_id, LocalAIErrorCode.CREDENTIAL_UNAVAILABLE)
        if secret_value is None:
            return error_response(request_id, LocalAIErrorCode.NOT_CONFIGURED)
        secret = secret_value.reveal()
        try:
            encoded = json.dumps(
                provider_http_body(request_value), ensure_ascii=False,
                allow_nan=False, separators=(",", ":"),
            ).encode("utf-8")
            http_request = Request(
                DEEPSEEK_ENDPOINT, data=encoded, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + secret},
            )
            with self._opener().open(
                    http_request, timeout=request_value["timeout_seconds"]) as response:
                raw = _read_bounded(response, MAX_PROVIDER_RESPONSE_BODY_BYTES)
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    return self._http_error(request_id, status)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or _contains_secret(payload, secret):
                raise ValueError
            return success_response(request_id, payload)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            return self._http_error(request_id, status)
        except (TimeoutError, socket.timeout):
            return error_response(request_id, LocalAIErrorCode.TIMEOUT)
        except URLError as exc:
            # urllib wraps socket timeouts in URLError on some Python/platform
            # combinations.  Preserve the bounded retry taxonomy without
            # exposing the provider's raw exception text.
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return error_response(request_id, LocalAIErrorCode.TIMEOUT)
            return error_response(request_id, LocalAIErrorCode.UNAVAILABLE)
        except (ssl.SSLError, OSError, http.client.HTTPException):
            return error_response(request_id, LocalAIErrorCode.UNAVAILABLE)
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError,
                RecursionError, CredentialStoreError):
            return error_response(request_id, LocalAIErrorCode.RESPONSE_INVALID)
        finally:
            secret = None

    def test_connection(self, *, timeout=20):
        """Minimal non-project request; DeepSeek has no assumed health endpoint."""
        request_id = str(uuid4())
        result = self.request({
            "request_id": request_id, "provider": PROVIDER, "model": MODEL,
            "messages": [
                {"role": "system", "content": "Connection test. Return one JSON object."},
                {"role": "user", "content": 'Reply with {"ok":true}.'},
            ],
            "parameters": {
                "response_format": {"type": "json_object"}, "max_tokens": 8,
                "thinking": {"type": "disabled"}, "temperature": 0,
                "stream": False,
            },
            "timeout_seconds": timeout,
            "metadata": {"prepare_request_id": None},
        })
        return result
