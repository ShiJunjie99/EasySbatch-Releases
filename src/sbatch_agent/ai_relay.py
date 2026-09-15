"""Single-user inference boundary. No project IO, job services, or SSH lifecycle."""

from contextlib import ExitStack
import os
import secrets
import threading
import time
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .analysis_context import AnalysisContext
from .model_client import (
    AnalysisOutputValidationError, ModelConfig, ModelErrorCode,
    ModelUnavailableError, OpenAICompatibleClient,
)
from .relay_client import REQUEST_LIMIT, json_bytes, relay_token, validate_request, validate_response
from .request_trace import attempt_scope, configure_logging, emit, request_scope, set_model


def create_relay_app(*, model_client=None):
    """Production always constructs the existing configured DeepSeek client.

    Explicit dependency injection is for offline tests only. The HTTP request
    cannot select a provider/model, endpoint, credentials, headers or tools.
    """
    token = relay_token()
    if model_client is None:
        config = ModelConfig.from_env()
        if config is None or config.provider != "deepseek":
            raise ValueError("Relay requires the configured local DeepSeek provider")
        model_client = OpenAICompatibleClient(config)
    if model_client.availability().state != "available":
        raise ModelUnavailableError(code=ModelErrorCode.CREDENTIAL_MISSING)
    key_env = getattr(getattr(model_client, "config", None), "api_key_env", None)
    provider_key = os.environ.get(key_env, "") if key_env else ""
    if provider_key and token == provider_key:
        raise ValueError("Relay authentication must be independent of provider authentication")
    protected = (token, provider_key)
    configure_logging()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"], www_redirect=False)
    busy = threading.Lock()

    def reply(data, status=200):
        return JSONResponse(data, status_code=status, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    @app.get("/health")
    async def health():
        # No upstream call, detailed configuration, identity or credential.
        return reply({"status": "ok"})

    @app.post("/v1/analyze")
    async def analyze(request: Request):
        started, outcome, failure = time.monotonic(), "failure", None
        acquired = False
        scope = ExitStack()
        authorized = secrets.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode())
        scope.enter_context(request_scope(fresh=True, incoming_id=request.headers.get("x-request-id") if authorized else None,
                                          protected=protected))
        set_model(model_client)
        try:
            if not authorized:
                failure = ModelUnavailableError(code=ModelErrorCode.AUTHENTICATION, origin="relay")
                return reply({"error": "relay_authentication_failure"}, 401)
            if request.headers.get("origin") or request.headers.get("sec-fetch-site"):
                return reply({"error": "browser_request_not_allowed"}, 403)
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return reply({"error": "invalid_request"}, 415)
            attempt = request.headers.get("x-model-attempt", "1")
            budget = request.headers.get("x-model-budget-ms", "90000")
            if attempt not in {"1", "2"} or not budget.isascii() or not budget.isdecimal() or not 1 <= len(budget) <= 6 or not 0 < int(budget) <= 120000:
                return reply({"error": "invalid_request"}, 400)
            scope.enter_context(attempt_scope(int(attempt), int(budget) / 1000))
            # Bound concurrent body buffers and upstream spend; no retries/queue.
            acquired = busy.acquire(blocking=False)
            if not acquired:
                failure = ModelUnavailableError(code=ModelErrorCode.RATE_LIMIT, origin="relay")
                return reply({"error": "relay_busy"}, 429)
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > REQUEST_LIMIT:
                    return reply({"error": "request_too_large"}, 413)
                raw.extend(chunk)
            try:
                value = validate_request(json.loads(raw))
                # Neither authentication secret belongs in a model prompt.
                text = json_bytes(value).decode()
                if any(secret and secret in text for secret in protected):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return reply({"error": "invalid_request"}, 400)
            result = await run_in_threadpool(
                model_client.generate_structured,
                context=AnalysisContext(value["system"], value["user"], (), ()), schema=value["schema"],
            )
            envelope = {"data": result.data, "request_id": result.request_id}
            validate_response(envelope, protected)
            outcome = "success"
            return reply(envelope)
        except AnalysisOutputValidationError as exc:
            failure = exc
            return reply({"error": "invalid_structured_response"}, 502)
        except ModelUnavailableError as exc:
            failure = exc
            code = exc.code if isinstance(exc.code, ModelErrorCode) else ModelErrorCode.UNAVAILABLE
            status = 504 if code == ModelErrorCode.TIMEOUT else 429 if code == ModelErrorCode.RATE_LIMIT else 502
            # Normalized types only: don't trust mutated future/custom errors.
            safe = ModelUnavailableError(code=code, http_status=exc.http_status, failure_kind=exc.failure_kind)
            return reply({"error": code.value, "http_status": safe.http_status, "failure_kind": safe.failure_kind}, status)
        except Exception as exc:
            # No exception text/traceback: even injected or future client errors
            # can contain an Authorization header, prompt or provider response.
            failure = exc
            return reply({"error": "relay_internal_error"}, 502)
        finally:
            if acquired:
                busy.release()
            emit("relay", status=outcome, duration_ms=(time.monotonic() - started) * 1000, exc=failure,
                 code="PREPARE_INPUT_INVALID" if failure is None and outcome != "success" else None)
            scope.close()

    return app
