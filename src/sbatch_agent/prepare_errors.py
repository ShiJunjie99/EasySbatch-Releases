"""Public Prepare error taxonomy. Never render exception text or provider bodies."""

from dataclasses import dataclass
from enum import StrEnum

from .model_client import AnalysisOutputValidationError, ModelErrorCode, ModelUnavailableError


class PrepareErrorCode(StrEnum):
    AI_NOT_CONFIGURED = "AI_NOT_CONFIGURED"
    AI_LOCAL_CREDENTIAL_UNAVAILABLE = "AI_LOCAL_CREDENTIAL_UNAVAILABLE"
    AI_PROVIDER_AUTH_FAILED = "AI_PROVIDER_AUTH_FAILED"
    AI_PROVIDER_RATE_LIMITED = "AI_PROVIDER_RATE_LIMITED"
    AI_PROVIDER_RESPONSE_INVALID = "AI_PROVIDER_RESPONSE_INVALID"
    AI_PROVIDER_IDENTITY_MISMATCH = "AI_PROVIDER_IDENTITY_MISMATCH"
    AI_CLIENT_DISCONNECTED = "AI_CLIENT_DISCONNECTED"
    AI_RELAY_UNAVAILABLE = "AI_RELAY_UNAVAILABLE"
    AI_PROVIDER_TIMEOUT = "AI_PROVIDER_TIMEOUT"
    AI_RATE_LIMITED = "AI_RATE_LIMITED"
    AI_PROVIDER_UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
    AI_CONFIGURATION_ERROR = "AI_CONFIGURATION_ERROR"
    AI_AUTHENTICATION_FAILED = "AI_AUTHENTICATION_FAILED"
    AI_TLS_ERROR = "AI_TLS_ERROR"
    AI_EGRESS_UNAVAILABLE = "AI_EGRESS_UNAVAILABLE"
    AI_EGRESS_AUTH_FAILED = "AI_EGRESS_AUTH_FAILED"
    AI_EGRESS_IDENTITY_MISMATCH = "AI_EGRESS_IDENTITY_MISMATCH"
    AI_EGRESS_TARGET_REJECTED = "AI_EGRESS_TARGET_REJECTED"
    AI_EGRESS_TLS_FAILED = "AI_EGRESS_TLS_FAILED"
    AI_EGRESS_TIMEOUT = "AI_EGRESS_TIMEOUT"
    AI_OUTPUT_INVALID = "AI_OUTPUT_INVALID"
    AI_OUTPUT_REJECTED = "AI_OUTPUT_REJECTED"
    CLUSTER_UNAVAILABLE = "CLUSTER_UNAVAILABLE"
    CATALOG_UNAVAILABLE = "CATALOG_UNAVAILABLE"
    RECOMMENDATION_UNAVAILABLE = "RECOMMENDATION_UNAVAILABLE"
    PREPARE_INPUT_INVALID = "PREPARE_INPUT_INVALID"
    PREPARE_INTERNAL_ERROR = "PREPARE_INTERNAL_ERROR"


def is_retryable_model_error(exc):
    """Positive allowlist only. Unknown errors/relay HTTP 5xx alone are NOT proof."""
    if not isinstance(exc, ModelUnavailableError):
        return False
    if exc.code in {ModelErrorCode.EGRESS_TIMEOUT, ModelErrorCode.EGRESS_UNAVAILABLE}:
        return exc.origin == "egress"
    if exc.code in {ModelErrorCode.TIMEOUT, ModelErrorCode.RATE_LIMIT}:
        return True
    return exc.code == ModelErrorCode.UNAVAILABLE and (
        exc.failure_kind in {"connection_refused", "connection_reset", "temporary_dns"}
        or (exc.origin in {"provider", "local_provider"} and
            type(exc.http_status) is int and 500 <= exc.http_status <= 599)
    )


def error_code(exc):
    if isinstance(exc, AnalysisOutputValidationError):
        rejected = exc.stage == "evidence" or exc.reason in {
            "inconsistent_proposal", "unknown_evidence", "unobserved_path", "unsupported_value",
        }
        return PrepareErrorCode.AI_OUTPUT_REJECTED if rejected else PrepareErrorCode.AI_OUTPUT_INVALID
    if isinstance(exc, ModelUnavailableError):
        if exc.origin == "local_provider":
            if exc.code == ModelErrorCode.AUTHENTICATION:
                return PrepareErrorCode.AI_PROVIDER_AUTH_FAILED
            if exc.code == ModelErrorCode.RATE_LIMIT:
                return PrepareErrorCode.AI_PROVIDER_RATE_LIMITED
        return {
            ModelErrorCode.LOCAL_NOT_CONFIGURED: PrepareErrorCode.AI_NOT_CONFIGURED,
            ModelErrorCode.LOCAL_CREDENTIAL_UNAVAILABLE: PrepareErrorCode.AI_LOCAL_CREDENTIAL_UNAVAILABLE,
            ModelErrorCode.LOCAL_IDENTITY_MISMATCH: PrepareErrorCode.AI_PROVIDER_IDENTITY_MISMATCH,
            ModelErrorCode.LOCAL_CLIENT_DISCONNECTED: PrepareErrorCode.AI_CLIENT_DISCONNECTED,
            ModelErrorCode.PROVIDER_RESPONSE_INVALID: PrepareErrorCode.AI_PROVIDER_RESPONSE_INVALID,
            ModelErrorCode.NOT_CONFIGURED: PrepareErrorCode.AI_CONFIGURATION_ERROR,
            ModelErrorCode.INVALID_CONFIG: PrepareErrorCode.AI_CONFIGURATION_ERROR,
            ModelErrorCode.CREDENTIAL_MISSING: PrepareErrorCode.AI_CONFIGURATION_ERROR,
            ModelErrorCode.AUTHENTICATION: PrepareErrorCode.AI_AUTHENTICATION_FAILED,
            ModelErrorCode.TLS_CERTIFICATE: PrepareErrorCode.AI_TLS_ERROR,
            ModelErrorCode.TIMEOUT: PrepareErrorCode.AI_PROVIDER_TIMEOUT,
            ModelErrorCode.RATE_LIMIT: PrepareErrorCode.AI_RATE_LIMITED,
            ModelErrorCode.EGRESS_UNAVAILABLE: PrepareErrorCode.AI_EGRESS_UNAVAILABLE,
            ModelErrorCode.EGRESS_AUTHENTICATION: PrepareErrorCode.AI_EGRESS_AUTH_FAILED,
            ModelErrorCode.EGRESS_IDENTITY: PrepareErrorCode.AI_EGRESS_IDENTITY_MISMATCH,
            ModelErrorCode.EGRESS_TARGET: PrepareErrorCode.AI_EGRESS_TARGET_REJECTED,
            ModelErrorCode.EGRESS_TLS: PrepareErrorCode.AI_EGRESS_TLS_FAILED,
            ModelErrorCode.EGRESS_TIMEOUT: PrepareErrorCode.AI_EGRESS_TIMEOUT,
        }.get(exc.code, PrepareErrorCode.AI_RELAY_UNAVAILABLE if exc.origin == "relay"
              else PrepareErrorCode.AI_PROVIDER_UNAVAILABLE)
    # Local imports keep transport independent of orchestration and its models.
    from .analysis_context import AnalysisInputError
    from .scanner import ProjectScanError
    from .server_catalog import CatalogError
    from .cluster import ClusterUnavailableError
    from .project_checks import ProjectChangedError
    from .smart_service import PreparationError
    from .renderer import JobSpecValidationError
    from pydantic import ValidationError
    if isinstance(exc, CatalogError):
        return PrepareErrorCode.CATALOG_UNAVAILABLE
    if isinstance(exc, ClusterUnavailableError):
        return PrepareErrorCode.CLUSTER_UNAVAILABLE
    if isinstance(exc, (AnalysisInputError, ProjectScanError, ProjectChangedError,
                        PreparationError, JobSpecValidationError, ValidationError)):
        return PrepareErrorCode.PREPARE_INPUT_INVALID
    return PrepareErrorCode.PREPARE_INTERNAL_ERROR


@dataclass(frozen=True)
class PrepareFailure:
    code: PrepareErrorCode
    title: str
    message: str
    http_status: int
    retry_analysis: bool = False


def present_failure(exc):
    code = error_code(exc)
    messages = {
        PrepareErrorCode.AI_NOT_CONFIGURED: ("AI 尚未配置", "请在 EasySbatch Launcher 中配置自己的 DeepSeek API Key；手动模式仍可使用。", 503),
        PrepareErrorCode.AI_LOCAL_CREDENTIAL_UNAVAILABLE: ("AI 凭据存储不可用", "未检测到可用的系统安全凭据存储；可选择仅本次运行使用。", 503),
        PrepareErrorCode.AI_PROVIDER_AUTH_FAILED: ("DeepSeek API Key 无效", "DeepSeek API Key 无效或已失效，请重新配置。", 503),
        PrepareErrorCode.AI_PROVIDER_RATE_LIMITED: ("AI 请求较多", "AI 服务当前请求较多，请稍后重试。", 429),
        PrepareErrorCode.AI_PROVIDER_RESPONSE_INVALID: ("AI 响应无效", "DeepSeek 返回的响应无法通过协议校验；请稍后重试。", 502),
        PrepareErrorCode.AI_PROVIDER_IDENTITY_MISMATCH: ("AI 会话校验失败", "当前 AI Provider 不属于该会话，已拒绝使用。", 403),
        PrepareErrorCode.AI_CLIENT_DISCONNECTED: ("本机 AI 客户端未连接", "请保持当前用户的 EasySbatch Launcher 运行；手动模式仍可使用。", 503),
        PrepareErrorCode.AI_RELAY_UNAVAILABLE: ("AI relay unavailable", "The secure AI connection is unavailable. Please check the relay/tunnel before trying analysis again.", 503),
        PrepareErrorCode.AI_PROVIDER_TIMEOUT: ("Temporary AI service issue", "AI analysis timed out within the configured request budget.", 504),
        PrepareErrorCode.AI_RATE_LIMITED: ("Temporary AI service issue", "The AI service is rate limited or has reached its quota. Please wait or check quota before trying again.", 429),
        PrepareErrorCode.AI_PROVIDER_UNAVAILABLE: ("AI analysis unavailable", "The configured AI provider could not complete this request.", 503),
        PrepareErrorCode.AI_CONFIGURATION_ERROR: ("AI configuration unavailable", "Ask the service maintainer to check provider/model configuration and credential readiness.", 503),
        PrepareErrorCode.AI_AUTHENTICATION_FAILED: ("AI authentication failed", "Ask the service maintainer to check AI access permissions. This error is not automatically retried.", 503),
        PrepareErrorCode.AI_TLS_ERROR: ("AI secure connection unavailable", "Certificate verification failed. Ask the service maintainer to check the existing trust configuration; verification has not been disabled.", 503),
        PrepareErrorCode.AI_EGRESS_UNAVAILABLE: ("AI 分析当前不可用", "请检查本机网络后重新连接 EasySbatch；你仍可以使用手动模式。", 503),
        PrepareErrorCode.AI_EGRESS_AUTH_FAILED: ("AI 出口认证失败", "请重新连接 EasySbatch；你仍可以使用手动模式。", 503),
        PrepareErrorCode.AI_EGRESS_IDENTITY_MISMATCH: ("AI 会话校验失败", "当前 AI 出口不属于该会话，已拒绝使用。", 403),
        PrepareErrorCode.AI_EGRESS_TARGET_REJECTED: ("AI 目标被拒绝", "AI 出口仅允许连接配置的 DeepSeek HTTPS 端点。", 403),
        PrepareErrorCode.AI_EGRESS_TLS_FAILED: ("AI 安全连接不可用", "DeepSeek 证书校验失败；未关闭 TLS 验证。", 503),
        PrepareErrorCode.AI_EGRESS_TIMEOUT: ("AI 网络暂时不可用", "当前会话的 AI 网络连接超时；你仍可以使用手动模式。", 504),
        PrepareErrorCode.AI_OUTPUT_INVALID: ("AI structured output invalid", "The response did not meet the structured output contract. Manual review is required; it was not repaired or retried.", 502),
        PrepareErrorCode.AI_OUTPUT_REJECTED: ("AI analysis rejected", "The proposed result conflicted with the project evidence or semantic contract. Manual review is required; the Harness has not been bypassed.", 422),
        PrepareErrorCode.CATALOG_UNAVAILABLE: ("Server software catalog unavailable", "Ask the service maintainer to check the catalog. Manual Mode can use existing valid registered profiles; no software paths are guessed.", 503),
        PrepareErrorCode.PREPARE_INPUT_INVALID: ("Check the project and task description", "Provide an accessible project directory and a non-empty task description. Review the inputs before analyzing again.", 400),
        PrepareErrorCode.PREPARE_INTERNAL_ERROR: ("Task preparation unavailable", "An unexpected preparation error occurred. Give the request ID to the service maintainer.", 500),
    }
    title, message, status = messages.get(code, messages[PrepareErrorCode.PREPARE_INTERNAL_ERROR])
    return PrepareFailure(code, title, message, status, is_retryable_model_error(exc))
