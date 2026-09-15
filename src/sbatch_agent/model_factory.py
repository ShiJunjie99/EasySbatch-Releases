"""Explicit provider and exact-session transport selection.

The B7 local-user provider keeps the prompt/Harness on the server while the
current user's Launcher performs fixed DeepSeek HTTPS with its OS credential.
Legacy relay/SSH egress modes remain opt-in research paths; no mode falls back
to another user/session or silently changes network placement.
"""

import os

from .ai_egress import AIEgressError, AIEgressHTTPTransport, AuditedAIEgressModelClient
from .model_client import (
    ModelConfig, ModelErrorCode, ModelUnavailableError, OpenAICompatibleClient,
)
from .relay_client import RelayConfig, RemoteRelayModelClient
from .local_user_model_client import LocalUserProviderConfig, LocalUserProviderModelClient


AI_TRANSPORT_MODES = frozenset({
    "direct", "legacy_relay", "per_user_ssh_egress", "structured_ssh_egress",
    "local_user_provider",
})


def model_transport_mode_from_env():
    explicit = (os.environ.get("SBATCH_AGENT_AI_TRANSPORT_MODE") or
                os.environ.get("AI_TRANSPORT_MODE"))
    if explicit:
        if explicit not in AI_TRANSPORT_MODES:
            raise ValueError("Invalid AI transport mode")
        return explicit
    return ("legacy_relay" if os.environ.get("SBATCH_AGENT_AI_PROVIDER") == "relay"
            else "direct")


def model_client_from_env(*, ai_egress=None, worker_session_id=None,
                          username=None, audit_session_id=None):
    mode = model_transport_mode_from_env()
    if mode == "legacy_relay":
        if os.environ.get("SBATCH_AGENT_AI_PROVIDER") != "relay":
            raise ValueError("legacy_relay requires the relay provider")
        return RemoteRelayModelClient(RelayConfig.from_env())
    if mode == "local_user_provider":
        if ai_egress is None:
            raise ModelUnavailableError(code=ModelErrorCode.LOCAL_CLIENT_DISCONNECTED,
                                        origin="local_provider")
        if not isinstance(worker_session_id, str) or not isinstance(username, str):
            raise ModelUnavailableError(code=ModelErrorCode.LOCAL_IDENTITY_MISMATCH,
                                        origin="local_provider")
        try:
            ai_egress.assert_binding(worker_session_id=worker_session_id, username=username)
        except Exception as exc:
            if getattr(exc, "code", "") in {"AI_PROVIDER_IDENTITY_MISMATCH",
                                              "WORKER_IDENTITY_MISMATCH"}:
                raise ModelUnavailableError(code=ModelErrorCode.LOCAL_IDENTITY_MISMATCH,
                                            origin="local_provider") from None
            raise ModelUnavailableError(code=ModelErrorCode.LOCAL_CLIENT_DISCONNECTED,
                                        origin="local_provider") from None
        return LocalUserProviderModelClient(
            LocalUserProviderConfig.from_env(), session=ai_egress,
            audit_session_id=audit_session_id, username=username,
        )
    config = ModelConfig.from_env()
    if config is None:
        return None
    if mode == "direct":
        return OpenAICompatibleClient(config)

    if config.provider != "deepseek":
        raise ModelUnavailableError(code=ModelErrorCode.INVALID_CONFIG)
    if ai_egress is None:
        raise ModelUnavailableError(
            code=ModelErrorCode.EGRESS_UNAVAILABLE, origin="egress",
        )
    if not isinstance(worker_session_id, str) or not isinstance(username, str):
        raise ModelUnavailableError(
            code=ModelErrorCode.EGRESS_IDENTITY, origin="egress",
        )
    try:
        ai_egress.assert_binding(
            worker_session_id=worker_session_id, username=username,
        )
    except AIEgressError as exc:
        raise OpenAICompatibleClient._egress_error(exc) from None
    if not ai_egress.available:
        raise ModelUnavailableError(
            code=ModelErrorCode.EGRESS_UNAVAILABLE, origin="egress",
        )
    client = OpenAICompatibleClient(config, transport=AIEgressHTTPTransport(ai_egress))
    return AuditedAIEgressModelClient(
        client, session=ai_egress, audit_session_id=audit_session_id,
        username=username,
    )
