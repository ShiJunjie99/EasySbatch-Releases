"""Local, server-rendered UI over SubmissionService; never executes CLI itself."""

import logging
import os
import pwd
import re
import secrets
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .persistence import JobRepository, PersistenceError, RecordNotFoundError
from .profiles import StaticProfiles
from .renderer import JobSpecValidationError
from .service import (
    JobNotSubmittableError, JobNotSubmittedError, SubmissionService, SubmissionServiceError,
)
from .slurm import SlurmClient
from .cluster import ClusterService, ClusterUnavailableError
from .prepare_errors import present_failure
from .request_trace import configure_logging, emit, request_scope
from .models import Resources
from .recommender import ResourceRecommender, RecommendationInputError
from .recommendation_models import UserPreference
from .scanner import ProjectScanner, ProjectScanError
from .scanner_models import ProjectEvidence
from .analysis_context import AnalysisContextBuilder, AnalysisInputError, GROUPS
from .analysis_models import field_proposals
from .analyzer import AIProjectAnalyzer
from .smart_service import SmartJobService, PreparationError
from .smart_models import PreparationValues
from .workspace_browser import WorkspaceBrowser, FolderAccessError
from .project_checks import ProjectChangedError
from .smart_web import PreparedStore, PreparedStateError, page_context, user_patch
from .smart_web import software_choices, catalog_environment_choices
from .server_catalog import ServerCatalog, provenance, compatibility
from .environment_resolver import EnvironmentResolver
from .model_client import (
    AnalysisOutputValidationError, ModelAvailability, ModelConfig,
    ModelErrorCode, ModelUnavailableError, OpenAICompatibleClient,
)
from .local_user_model_client import LocalUserProviderConfig, LocalUserProviderModelClient
from .model_factory import model_client_from_env, model_transport_mode_from_env
from .ai_egress import configure_ai_egress_audit
from .web_forms import (
    DEFAULT_FORM, FormError, profile_choices, spec_from_form, validation_messages,
    recommendation_request_from_form, apply_resources_to_form,
    apply_catalog_to_form, manual_resource_recommendations, _integer,
)
from .presentation import (build_file_tree, field_source_badge, status_badge, snapshot_label,
                           REVIEW_GROUPS, catalog_details, PLACEHOLDERS, project_summary, compact_review_values,
                           verification_label, file_tag_label, run_type_label, preference_label,
                           queue_reason_label, compact_executable, prepare_failure_copy, error_display)
from .ui_formatting import field_label, format_args, format_duration, display_form
from .ssh_poc import SSHProbeError
from .ssh_poc_deployment import SSH_HOST, SSH_PORT
from .cluster_profile import ClusterProfile
from .web_sessions import (
    AuthenticationBoundaryMiddleware, COOKIE_NAME, SSH_FIRST_COOKIE_NAME, ServerSessionMiddleware,
    SessionManager, configure_authentication_audit, emit_auth_event,
)
from .web_ssh_auth import LoginCredentials, SSHPasswordAuthenticator
from .worker_broker import WorkerBroker, WorkerBrokerError


logger = logging.getLogger(__name__)
MAX_FORM_BYTES = 128 * 1024
APPLY_MAX_AGE_SECONDS = 900


@dataclass(frozen=True)
class WebConfig:
    database_path: Path
    runs_root: Path
    profiles_path: Path | None = None
    catalog_path: Path | None = None
    workspace_root: Path | None = None
    max_entries_per_directory: int = 200
    authentication_enabled: bool = False
    ssh_host: str = SSH_HOST
    ssh_port: int = SSH_PORT
    ssh_known_hosts: str | None = None
    session_idle_timeout_seconds: int = 1800
    session_cookie_secure: bool = False
    deployment_mode: str = "loopback_legacy"
    public_base_url: str | None = None
    worker_broker_socket: str | None = None

    def __post_init__(self):
        if (type(self.authentication_enabled) is not bool or
                type(self.session_cookie_secure) is not bool or
                type(self.session_idle_timeout_seconds) is not int or
                not 60 <= self.session_idle_timeout_seconds <= 86400 or
                self.deployment_mode not in {"loopback_legacy", "ssh_first", "lan_https"}):
            raise ValueError("Web authentication configuration is invalid")
        ClusterProfile("configured-cluster", "Configured cluster", self.ssh_host, self.ssh_port)
        if self.deployment_mode == "lan_https":
            try:
                public_url = urlsplit(self.public_base_url or "")
                public_port = public_url.port
            except ValueError:
                raise ValueError("LAN HTTPS public URL is invalid") from None
            if (not self.authentication_enabled or not self.session_cookie_secure or
                    public_url.scheme != "https" or public_url.hostname != self.ssh_host or
                    public_port is None or not 1024 <= public_port <= 65535 or
                    public_url.path not in {"", "/"} or public_url.query or public_url.fragment or
                    public_url.username is not None or public_url.password is not None):
                raise ValueError("LAN HTTPS deployment configuration is invalid")
            if self.worker_broker_socket not in {None, ""}:
                raise ValueError("LAN HTTPS does not use a worker broker")
        elif self.public_base_url not in {None, ""}:
            raise ValueError("Loopback modes do not use a public base URL")
        if self.deployment_mode == "ssh_first":
            if (not self.authentication_enabled or self.session_cookie_secure or
                    not isinstance(self.worker_broker_socket, str) or
                    re.fullmatch(r"/tmp/easysbatch-[0-9]+/broker\.sock",
                                 self.worker_broker_socket) is None):
                raise ValueError("SSH-first deployment configuration is invalid")
        elif self.worker_broker_socket not in {None, ""}:
            raise ValueError("Worker broker is only available in SSH-first mode")

    @property
    def trusted_hosts(self):
        hosts = ["localhost", "127.0.0.1"]
        if self.deployment_mode == "lan_https":
            hosts.append(self.ssh_host)
        return tuple(hosts)

    @property
    def session_cookie_name(self):
        return SSH_FIRST_COOKIE_NAME if self.deployment_mode == "ssh_first" else COOKIE_NAME

    @classmethod
    def from_env(cls):
        profile_path = os.environ.get("SBATCH_AGENT_PROFILES_PATH")
        catalog_path = os.environ.get("SBATCH_AGENT_SERVER_CATALOG_PATH")
        def boolean(name, default="0"):
            value = os.environ.get(name, default)
            if value not in {"0", "1"}:
                raise ValueError(f"{name} must be 0 or 1")
            return value == "1"

        known_hosts = os.environ.get("SBATCH_AGENT_SSH_KNOWN_HOSTS")
        return cls(
            Path(os.environ.get("SBATCH_AGENT_DATABASE_PATH", ".sbatch-agent/jobs.sqlite3")).absolute(),
            Path(os.environ.get("SBATCH_AGENT_RUNS_ROOT", ".sbatch-agent/runs")).absolute(),
            Path(profile_path).absolute() if profile_path else None,
            Path(catalog_path).absolute() if catalog_path else None,
            Path(os.environ["SBATCH_AGENT_WORKSPACE_ROOT"]) if os.environ.get("SBATCH_AGENT_WORKSPACE_ROOT") else None,
            int(os.environ.get("SBATCH_AGENT_FOLDER_MAX_ENTRIES", "200")),
            boolean("SBATCH_AGENT_WEB_AUTH_ENABLED"),
            os.environ.get("SBATCH_AGENT_SSH_HOST", SSH_HOST),
            int(os.environ.get("SBATCH_AGENT_SSH_PORT", str(SSH_PORT))),
            known_hosts or None,
            int(os.environ.get("SBATCH_AGENT_SESSION_IDLE_SECONDS", "1800")),
            boolean("SBATCH_AGENT_SESSION_COOKIE_SECURE"),
            os.environ.get("SBATCH_AGENT_DEPLOYMENT_MODE", "loopback_legacy"),
            os.environ.get("SBATCH_AGENT_PUBLIC_BASE_URL") or None,
            os.environ.get("SBATCH_AGENT_WORKER_BROKER_SOCKET") or None,
        )


def create_app(
    config: WebConfig | None = None, *, profiles: StaticProfiles | None = None,
    slurm_client: SlurmClient | None = None, cluster_service: ClusterService | None = None,
    recommender: ResourceRecommender | None = None,
    project_scanner: ProjectScanner | None = None,
    project_analyzer: AIProjectAnalyzer | None = None,
    catalog: ServerCatalog | None = None,
    ssh_authenticator=None, session_manager: SessionManager | None = None,
    worker_broker=None,
) -> FastAPI:
    """Factory with path/profile/client injection, no import-time resources.

    Every operation opens, uses and closes its repository in one worker thread.
    Only explicit POST actions invoke the job client. GET /cluster performs one
    read-only snapshot through the separate cluster service.
    """
    configure_logging()
    configure_authentication_audit()
    configure_ai_egress_audit()
    config = config or WebConfig.from_env()
    if not config.database_path.is_absolute() or not config.runs_root.is_absolute():
        raise ValueError("Web database_path and runs_root must be absolute paths")
    folders = WorkspaceBrowser(config.workspace_root or Path.cwd(),
                               max_entries_per_directory=config.max_entries_per_directory)
    if profiles is None:
        # No guessed default environment. The UI can view existing records with
        # no configured profiles, but cannot create a job until one is selected.
        profiles = (StaticProfiles.model_validate(yaml.safe_load(
            config.profiles_path.read_text(encoding="utf-8")
        )) if config.profiles_path else StaticProfiles())
    profiles = StaticProfiles.model_validate(profiles.model_dump())
    if catalog is None and config.catalog_path:
        catalog = ServerCatalog.load(config.catalog_path, profiles=profiles)
    if catalog is not None:
        catalog = ServerCatalog.model_validate(catalog.model_dump()).validate_profiles(profiles)
    client = slurm_client if slurm_client is not None else SlurmClient()
    cluster = cluster_service if cluster_service is not None else ClusterService()
    advisor = recommender if recommender is not None else ResourceRecommender()
    scanner = project_scanner if project_scanner is not None else ProjectScanner()
    analyzer = project_analyzer
    transport_mode = "invalid"
    configured_client = None
    unavailable_status = ModelAvailability("not_configured")
    local_provider_config = None
    if analyzer is None:
        try:
            transport_mode = model_transport_mode_from_env()
            if transport_mode == "local_user_provider":
                local_provider_config = LocalUserProviderConfig.from_env()
                unavailable_status = ModelAvailability("not_configured", local_provider_config.model)
            elif transport_mode in {"per_user_ssh_egress", "structured_ssh_egress"}:
                configured = ModelConfig.from_env()
                if configured is not None:
                    configured_client = OpenAICompatibleClient(configured)
                    unavailable_status = configured_client.availability()
            else:
                configured_client = model_client_from_env()
                if configured_client is not None:
                    analyzer = AIProjectAnalyzer(model_client=configured_client, profiles=profiles,
                                                 environment_resolver=EnvironmentResolver(catalog))
        except (ValueError, TypeError, ModelUnavailableError):
            # Optional AI misconfiguration must not prevent manual app startup.
            configured_client = None
            analyzer = None
            unavailable_status = ModelAvailability("invalid_configuration")
            logger.warning("AI configuration unavailable; manual mode remains enabled")
    else:
        # Explicit test/deployment injection is authoritative.
        transport_mode = "injected"

    def ai_availability(request=None):
        if transport_mode == "local_user_provider":
            record = request.scope.get("server_session") if request is not None else None
            provider = record.ai_egress if record is not None and record.authenticated else None
            if provider is None:
                return unavailable_status
            status = getattr(provider, "status", {})
            if provider.available:
                state = "available"
            elif status.get("backend") == "Unavailable":
                state = "unavailable"
            else:
                state = "not_configured"
            return ModelAvailability(state, local_provider_config.model if local_provider_config else None)
        if transport_mode in {"per_user_ssh_egress", "structured_ssh_egress"}:
            if unavailable_status.state in {
                    "not_configured", "invalid_configuration", "credential_missing"}:
                return unavailable_status
            record = request.scope.get("server_session") if request is not None else None
            egress = record.ai_egress if record is not None and record.authenticated else None
            return ModelAvailability(
                "available" if egress is not None and egress.available else "unavailable",
                configured_client.model if configured_client is not None else None,
            )
        if configured_client is not None:
            return configured_client.availability()
        if analyzer is not None:
            # Injected analyzer wins over provider environment configuration.
            return ModelAvailability("available", analyzer.model_client.model)
        return unavailable_status

    def analyzer_for_request(request):
        if project_analyzer is not None:
            return project_analyzer
        if transport_mode == "local_user_provider":
            record = request.scope.get("server_session")
            if record is None or not record.authenticated or record.ai_egress is None:
                raise ModelUnavailableError(code=ModelErrorCode.LOCAL_CLIENT_DISCONNECTED,
                                            origin="local_provider")
            context = record.ssh_context
            if local_provider_config is None:
                raise ModelUnavailableError(code=ModelErrorCode.INVALID_CONFIG,
                                            origin="local_provider")
            return AIProjectAnalyzer(
                model_client=LocalUserProviderModelClient(
                    local_provider_config, session=record.ai_egress,
                    audit_session_id=record.audit_session_id,
                    username=record.identity.username,
                ), profiles=profiles, environment_resolver=EnvironmentResolver(catalog),
            )
        if transport_mode not in {"per_user_ssh_egress", "structured_ssh_egress"}:
            if analyzer is None:
                raise ModelUnavailableError(
                    code=(ModelErrorCode.INVALID_CONFIG
                          if unavailable_status.state == "invalid_configuration"
                          else ModelErrorCode.NOT_CONFIGURED),
                )
            return analyzer
        record = request.scope.get("server_session")
        if record is None or not record.authenticated:
            raise ModelUnavailableError(
                code=ModelErrorCode.EGRESS_UNAVAILABLE, origin="egress",
            )
        context = record.ssh_context
        client_for_session = model_client_from_env(
            ai_egress=record.ai_egress,
            worker_session_id=getattr(context, "worker_id", None),
            username=record.identity.username,
            audit_session_id=record.audit_session_id,
        )
        if client_for_session is None:
            raise ModelUnavailableError(code=ModelErrorCode.NOT_CONFIGURED)
        return AIProjectAnalyzer(
            model_client=client_for_session, profiles=profiles,
            environment_resolver=EnvironmentResolver(catalog),
        )
    config.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with JobRepository(config.database_path) as repository:
        # Fail at startup on incompatible DB/root configuration, without CLI I/O.
        SubmissionService(repository=repository, slurm_client=client, profiles=profiles,
                          submission_root=config.runs_root)

    sessions = session_manager or SessionManager(
        idle_timeout_seconds=config.session_idle_timeout_seconds,
    )
    authenticator = ssh_authenticator
    if (config.authentication_enabled and config.deployment_mode != "ssh_first" and
            authenticator is None):
        authenticator = SSHPasswordAuthenticator(
            host=config.ssh_host, port=config.ssh_port,
            known_hosts=config.ssh_known_hosts,
        )
    broker = worker_broker
    if config.deployment_mode == "ssh_first" and broker is None:
        broker = WorkerBroker(config.worker_broker_socket)

    @asynccontextmanager
    async def lifespan(_app):
        if broker is not None:
            await run_in_threadpool(broker.start)
        try:
            yield
        finally:
            await run_in_threadpool(sessions.close_all)
            if broker is not None:
                await run_in_threadpool(broker.stop)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    assets = Path(__file__).parent
    templates = Jinja2Templates(directory=assets / "templates")
    templates.env.globals.update(status_badge=status_badge, field_source_badge=field_source_badge,
                                 snapshot_label=snapshot_label, review_groups=REVIEW_GROUPS,
                                 placeholders=PLACEHOLDERS, project_summary=project_summary,
                                 verification_label=verification_label, file_tag_label=file_tag_label,
                                 run_type_label=run_type_label, preference_label=preference_label,
                                 queue_reason_label=queue_reason_label, compact_executable=compact_executable,
                                 prepare_failure_copy=prepare_failure_copy, field_label=field_label,
                                 format_args=format_args, format_duration=format_duration,
                                 error_display=error_display)
    app.mount("/static", StaticFiles(directory=assets / "static"), name="static")
    process_identity = pwd.getpwuid(os.geteuid()).pw_name
    # The ephemeral signing key only protects CSRF/session notices; no JobRecord
    # lives in the session. Reloading the app invalidates old forms, not jobs.
    signing_key = secrets.token_urlsafe(32)
    choices = URLSafeTimedSerializer(signing_key, salt="recommendation-apply")
    scans = URLSafeTimedSerializer(signing_key, salt="analysis-evidence")
    smart = SmartJobService(analyzer=analyzer, cluster_service=cluster, profiles=profiles,
                           scanner=scanner, recommender=advisor, catalog=catalog)
    prepared_store = PreparedStore()
    app.state.prepared_store = prepared_store
    app.state.workspace_browser = folders
    app.state.session_manager = sessions
    app.state.ssh_authenticator = authenticator
    app.state.worker_broker = broker
    app.state.process_identity = process_identity
    app.state.ai_transport_mode = transport_mode

    def page(request, template, *, status_code=200, **context):
        token = request.session.setdefault("csrf", secrets.token_urlsafe(32))
        server_session = request.scope.get("server_session")
        authenticated = bool(server_session and server_session.authenticated)
        displayed_identity = (server_session.identity.username if authenticated else process_identity)
        legacy_allowed = not config.authentication_enabled or (
            authenticated and displayed_identity == process_identity
        )
        if is_partial(request) and template == "error.html":
            template = "partials/error.html"
        response = templates.TemplateResponse(request=request, name=template, context={
            "identity": displayed_identity, "csrf_token": token,
            "authenticated": authenticated,
            "legacy_allowed": legacy_allowed,
            "authentication_enabled": config.authentication_enabled,
            "ai_transport_mode": transport_mode,
            "ai_status": ai_availability(request), **context,
        }, status_code=status_code)
        response.headers["Vary"] = "HX-Request"
        return response

    def is_partial(request):
        return request.headers.get("hx-request", "").lower() == "true"

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; font-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        # no-referrer makes browsers send Origin: null on navigation POSTs,
        # conflicting with the exact-origin CSRF check below. same-origin
        # preserves local form origins while withholding cross-origin referrers.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        if config.deployment_mode == "ssh_first" and transport_mode in {
                "local_user_provider", "per_user_ssh_egress", "structured_ssh_egress"}:
            response.headers["X-EasySbatch-AI-Egress"] = (
                "local" if transport_mode == "local_user_provider" else
                "v2" if transport_mode == "structured_ssh_egress" else "v1"
            )
        return response

    async def form_data(request: Request) -> dict[str, str]:
        # Plain HTML forms only: bounded standard URL-encoding, no file uploads
        # or multipart dependency. Keep argument text unchanged after decoding.
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "只接受普通 HTML 表单，不支持文件上传。")
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            raise HTTPException(403, "跨站表单请求已拒绝。")
        origin = request.headers.get("origin")
        expected_origin = (config.public_base_url or
                           f"{request.url.scheme}://{request.url.netloc}").rstrip("/")
        if origin is not None and origin.rstrip("/") != expected_origin:
            raise HTTPException(403, "跨站表单请求已拒绝。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_FORM_BYTES:
                raise HTTPException(413, "表单过大，请限制在 128 KiB 内。")
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True,
                              encoding="utf-8", errors="strict", max_num_fields=64)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "表单编码或字段数量不合法。") from exc
        form = dict(pairs)
        if len(pairs) != len(form):
            raise HTTPException(400, "表单包含重复字段。")
        expected, received = request.session.get("csrf"), form.pop("csrf_token", "")
        if not expected or not secrets.compare_digest(expected.encode(), received.encode()):
            raise HTTPException(403, "表单验证已失效，请重新打开页面后操作。")
        return form

    async def bootstrap_form_data(request: Request) -> dict[str, str]:
        """Bounded Launcher-only form; the one-time token is its CSRF proof."""
        if config.deployment_mode != "ssh_first":
            raise HTTPException(404, "SSH-first 启动入口未启用。")
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "启动请求格式不正确。")
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            raise HTTPException(403, "跨站启动请求已拒绝。")
        origin = request.headers.get("origin")
        expected_origin = f"http://{request.url.netloc}"
        if origin is not None and origin.rstrip("/") != expected_origin:
            raise HTTPException(403, "启动来源不正确。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "启动请求过大。")
        max_fields = 4 if transport_mode == "per_user_ssh_egress" else 2
        try:
            pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True,
                              encoding="ascii", errors="strict", max_num_fields=max_fields)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "启动请求无效。") from exc
        form = dict(pairs)
        body.clear()
        fields = set(form)
        allowed = ({"worker_id", "bootstrap_token"},)
        if transport_mode == "per_user_ssh_egress":
            allowed += ({"worker_id", "bootstrap_token",
                         "ai_egress_port", "ai_egress_credential"},)
        if len(pairs) != len(form) or fields not in allowed:
            form.clear()
            raise HTTPException(400, "启动请求无效。")
        return form

    def operation(method: str, *args, **kwargs):
        # Opening/closing inside this callback avoids passing SQLite connections
        # across FastAPI's event-loop, dependency and worker threads.
        with JobRepository(config.database_path) as repository:
            if method == "get":
                return repository.get(*args)
            if method == "list":
                return repository.list(limit=100)
            service = SubmissionService(repository=repository, slurm_client=client,
                                        profiles=profiles, submission_root=config.runs_root)
            return getattr(service, method)(*args, **kwargs)

    def check_id(record_id: str):
        try:
            if str(UUID(record_id)) != record_id:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(404, "任务记录不存在。") from exc

    def new_page(request, form=None, errors=(), status_code=200, *, report=None, apply_tokens=None, notice=None,
                 project_evidence=None, scan_token=None, analysis=None, task_intent="", prepare_failure=None, prepare_trace=None,
                 selected_folder=None, folder_listing=None, folder_error=None):
        template = "new.html"
        if is_partial(request):
            template = {"/new/scan": "partials/scan_result.html",
                        "/new/prepare": "partials/prepare_error.html",
                        "/new/analyze": "partials/analysis.html"}.get(request.url.path, "partials/alerts.html")
        return page(request, template, status_code=status_code,
                    prepare_failure=prepare_failure, prepare_trace=prepare_trace,
                    selected_folder=selected_folder, folder_listing=folder_listing, folder_error=folder_error,
                    form=display_form(DEFAULT_FORM if form is None else form), errors=errors,
                    policy_recommendations=manual_resource_recommendations(DEFAULT_FORM if form is None else form, profiles, catalog),
                    environments=catalog_environment_choices(catalog, profiles),
                    software_options=software_choices(catalog.software) if catalog else [],
                    catalog_facts=(*catalog.software, *catalog.environments) if catalog else (),
                    launchers=profile_choices(profiles.launchers),
                    preferences=list(UserPreference), report=report, apply_tokens=apply_tokens or {}, notice=notice,
                    project_evidence=project_evidence, scan_token=scan_token, analysis=analysis,
                    file_tree=build_file_tree(project_evidence) if project_evidence else None,
                    task_intent=task_intent, ai_status=ai_availability(request),
                    manual_open=request.query_params.get("mode") == "manual" or request.method == "POST",
                    analysis_fields=list(field_proposals(analysis.draft)) if analysis else [])

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return page(request, "error.html", status_code=exc.status_code, message=exc.detail)

    @app.exception_handler(RecordNotFoundError)
    async def not_found(request, exc):
        return page(request, "error.html", status_code=404, message="任务记录不存在。")

    @app.exception_handler(PersistenceError)
    async def storage_error(request, exc):
        logger.error("Repository operation failed", exc_info=exc)
        return page(request, "error.html", status_code=503,
                    message="无法访问任务记录。若刚执行提交，请先核对记录与 Slurm，不要重新提交。")

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error("Unexpected web failure", exc_info=exc)
        return page(request, "error.html", status_code=500,
                    message="操作未能完成。若刚执行提交，请先核对 Slurm，避免重复作业。")

    def authenticated_session(request):
        record = request.scope.get("server_session")
        if record is None or not record.authenticated:
            raise HTTPException(401, "会话已失效，请重新登录。")
        return record

    @app.get("/login")
    async def login_page(request: Request):
        if not config.authentication_enabled:
            raise HTTPException(404, "登录功能未启用。")
        record = request.scope.get("server_session")
        if config.authentication_enabled and record is not None and record.authenticated:
            return RedirectResponse("/session", status_code=303)
        reason = request.query_params.get("reason")
        notice = ({
            "expired": "会话已失效，请重新登录。",
            "disconnected": "集群连接已断开，请重新登录。",
        }).get(reason)
        if config.deployment_mode == "ssh_first":
            notice = ({
                "expired": "会话已失效，请重新运行 EasySbatch Launcher。",
                "disconnected": "集群连接已断开，请重新运行 EasySbatch Launcher。",
            }).get(reason)
            return page(request, "ssh_first_login.html", notice=notice)
        return page(request, "login.html", notice=notice, username_value="")

    @app.post("/login")
    async def login(request: Request):
        if (not config.authentication_enabled or authenticator is None or
                config.deployment_mode == "ssh_first"):
            raise HTTPException(404, "登录功能未启用。")
        record = request.scope.get("server_session")
        if record is not None and record.authenticated:
            return RedirectResponse("/session", status_code=303)
        posted = await form_data(request)
        started = time.monotonic()
        correlation_id = str(uuid4())
        username_value = posted.get("username", "")
        credentials = context = None
        try:
            if set(posted) != {"username", "password"}:
                raise ValueError
            credentials = LoginCredentials.model_validate(posted)
            username_value = credentials.username
            context = await run_in_threadpool(authenticator.authenticate, credentials)
            if (context.identity.username != credentials.username or not context.connected):
                raise SSHProbeError("SSH_IDENTITY_MISMATCH")
            new_token, _ = sessions.promote(
                request.scope.get("session_token"), context,
                correlation_id=correlation_id,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            context = None  # ownership transferred to SessionManager
            request.scope["session_cookie_override"] = new_token
            return RedirectResponse("/session", status_code=303)
        except (ValidationError, ValueError):
            emit_auth_event(
                "LOGIN_FAILED", username=None, result="FAIL",
                correlation_id=correlation_id,
                duration_ms=round((time.monotonic() - started) * 1000),
                error_code="SSH_AUTH_FAILED",
            )
            return page(request, "login.html", status_code=401,
                        error="用户名或 SSH 密码不正确。", username_value=username_value)
        except SSHProbeError as exc:
            if exc.code == "SSH_IDENTITY_MISMATCH":
                event = "IDENTITY_MISMATCH"
                message = "集群身份校验失败，本次登录已终止。"
                status = 403
            elif exc.code == "SSH_HOST_KEY_FAILED":
                event = "LOGIN_FAILED"
                message = "无法验证集群服务器身份，请联系管理员。"
                status = 503
            elif exc.code == "SSH_AUTH_FAILED":
                event = "LOGIN_FAILED"
                message = "用户名或 SSH 密码不正确。"
                status = 401
            else:
                event = "LOGIN_FAILED"
                message = "暂时无法连接集群，请稍后再试。"
                status = 503
            emit_auth_event(
                event, username=username_value if username_value else None,
                result="FAIL", correlation_id=correlation_id,
                duration_ms=round((time.monotonic() - started) * 1000),
                error_code=exc.code,
            )
            return page(request, "login.html", status_code=status,
                        error=message, username_value=username_value)
        except Exception:
            emit_auth_event(
                "LOGIN_FAILED", username=username_value if username_value else None,
                result="FAIL", correlation_id=correlation_id,
                duration_ms=round((time.monotonic() - started) * 1000),
                error_code="SSH_CONNECTION_FAILED",
            )
            return page(request, "login.html", status_code=503,
                        error="暂时无法连接集群，请稍后再试。",
                        username_value=username_value)
        finally:
            if context is not None:
                context.close()
            posted.clear()
            credentials = None

    @app.post("/auth/ssh-bootstrap")
    async def ssh_bootstrap(request: Request):
        if config.deployment_mode != "ssh_first" or broker is None:
            raise HTTPException(404, "SSH-first 启动入口未启用。")
        record = request.scope.get("server_session")
        if record is not None and record.authenticated:
            return RedirectResponse("/session", status_code=303)
        posted = await bootstrap_form_data(request)
        context = None
        try:
            context = await run_in_threadpool(
                broker.consume_bootstrap, posted["worker_id"], posted["bootstrap_token"],
            )
            egress = None
            if "ai_egress_port" in posted:
                try:
                    port = int(posted["ai_egress_port"])
                    egress = context.attach_ai_egress(
                        remote_proxy_port=port,
                        credential=posted["ai_egress_credential"],
                    )
                    await run_in_threadpool(egress.health_check)
                except Exception:
                    # AI is optional. Keep the verified Worker/Web session and
                    # never borrow an egress from a different session.
                    logger.warning(
                        "AI egress unavailable user=%s egress_id=%s remote_port=%s",
                        context.identity.username,
                        getattr(egress, "egress_id", None),
                        getattr(egress, "remote_proxy_port", None),
                    )
            elif transport_mode == "structured_ssh_egress":
                try:
                    egress = context.attach_structured_ai_egress()
                    await run_in_threadpool(egress.health_check)
                except Exception:
                    logger.warning(
                        "Structured AI egress unavailable user=%s egress_id=%s",
                        context.identity.username, getattr(egress, "egress_id", None),
                    )
            elif transport_mode == "local_user_provider":
                try:
                    egress = context.attach_local_ai_provider(
                        audit_session_id=None,
                    )
                except Exception:
                    logger.warning("Local AI provider unavailable user=%s",
                                   context.identity.username)
            new_token, new_record = sessions.promote(
                request.scope.get("session_token"), context,
            )
            context = None
            if new_record.ai_egress is not None and getattr(new_record.ai_egress, "audit_session_id", None) is None:
                new_record.ai_egress.audit_session_id = new_record.audit_session_id
            request.scope["session_cookie_override"] = new_token
            response = RedirectResponse("/session", status_code=303)
            response.headers["X-EasySbatch-AI-Status"] = (
                "available" if new_record.ai_egress is not None and
                new_record.ai_egress.available else "unavailable"
            )
            return response
        except (WorkerBrokerError, ValueError):
            emit_auth_event("LOGIN_FAILED", username=None, result="FAIL",
                            error_code="SSH_CONNECTION_FAILED")
            return page(request, "ssh_first_login.html", status_code=401,
                        error="启动凭据已失效，请重新运行 EasySbatch Launcher。")
        finally:
            if context is not None:
                context.close()
            posted.clear()

    @app.get("/session")
    async def session_page(request: Request):
        record = authenticated_session(request)
        notice = request.session.pop("identity_notice", None)
        legacy_blocked = request.query_params.get("legacy") == "blocked"
        return page(request, "session.html", session_identity=record.identity,
                    connection_ok=record.ssh_context.connected,
                    notice=notice, legacy_blocked=legacy_blocked)

    @app.post("/session/verify")
    async def verify_session_identity(request: Request):
        record = authenticated_session(request)
        posted = await form_data(request)
        if posted:
            raise HTTPException(400, "身份验证操作不接受额外字段。")
        started = time.monotonic()
        correlation_id = str(uuid4())
        try:
            with record.operation_lock:
                current = await run_in_threadpool(record.ssh_context.verify_identity)
            if current.username != record.identity.username or current.uid != record.identity.uid:
                raise SSHProbeError("SSH_IDENTITY_MISMATCH")
            request.session["identity_notice"] = "集群身份已重新验证。"
            emit_auth_event(
                "IDENTITY_VERIFIED", audit_session_id=record.audit_session_id,
                username=record.identity.username, result="SUCCESS",
                correlation_id=correlation_id,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return RedirectResponse("/session", status_code=303)
        except (SSHProbeError, WorkerBrokerError) as exc:
            if exc.code in {"SSH_IDENTITY_MISMATCH", "WORKER_IDENTITY_MISMATCH"}:
                emit_auth_event(
                    "IDENTITY_MISMATCH", audit_session_id=record.audit_session_id,
                    username=record.identity.username, result="FAIL",
                    correlation_id=correlation_id, error_code=exc.code,
                )
            sessions.invalidate(request.scope.get("session_token"),
                                event=("WORKER_DISCONNECTED" if config.deployment_mode == "ssh_first"
                                       else "SSH_DISCONNECTED"),
                                correlation_id=correlation_id)
            return RedirectResponse("/login?reason=disconnected", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        authenticated_session(request)
        posted = await form_data(request)
        if posted:
            raise HTTPException(400, "退出登录操作不接受额外字段。")
        sessions.invalidate(request.scope.get("session_token"), event="LOGOUT")
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            config.session_cookie_name, path="/", secure=config.session_cookie_secure,
            httponly=True, samesite="strict",
        )
        return response

    @app.get("/")
    async def home(request: Request):
        return page(request, "home.html")

    @app.get("/new")
    async def new_job(request: Request):
        return new_page(request)

    def folder_page(request, *, listing=None, message=None, status=200):
        if not is_partial(request):
            return new_page(request, folder_listing=listing, folder_error=message, status_code=status)
        response = page(request, "partials/folder_picker.html", folder_listing=listing,
                        folder_error=message, status_code=status)
        response.headers["HX-Retarget"] = "#folder-picker"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    @app.get("/ui/folders")
    async def browse_folders(request: Request):
        try:
            pairs = list(request.query_params.multi_items())
            if len(pairs) > 1 or any(k != "path" for k, _ in pairs):
                raise FolderAccessError()
            listing = await run_in_threadpool(folders.browse, request.query_params.get("path", "."))
            return folder_page(request, listing=listing)
        except FolderAccessError as exc:
            return folder_page(request, message=str(exc), status=exc.status)

    @app.post("/ui/folders/select")
    async def select_folder(request: Request):
        try:
            posted = await form_data(request)
            if set(posted) - {"path", "task_intent"} or "path" not in posted:
                raise FolderAccessError()
            selected = await run_in_threadpool(folders.select, posted["path"])
            if is_partial(request):
                return page(request, "partials/working_folder.html", selected_folder=selected)
            return new_page(request, selected_folder=selected, task_intent=posted.get("task_intent", ""))
        except FolderAccessError as exc:
            return folder_page(request, message=str(exc), status=exc.status)
        except HTTPException as exc:
            return folder_page(request, message=exc.detail, status=exc.status_code)

    @app.exception_handler(PreparedStateError)
    async def prepared_state_error(request, exc):
        return page(request, "error.html", status_code=409, message=str(exc))

    def prepared_operation(record_id, owner, action="get", *, posted=None):
        # The browser never sends a trusted analysis/JobSpec. A short-lived
        # server entry serializes edits and Confirm within this Web worker.
        with prepared_store.use(record_id, owner) as entry:
            if action == "get":
                return deepcopy(entry.prepared), entry.attempted
            data = dict(posted or {})
            revision = data.pop("revision", "")
            if revision != str(entry.prepared.revision):
                raise PreparedStateError("页面版本已过期，请重新打开提交前确认页面。")
            if action == "finalize":
                if entry.attempted:
                    raise PreparedStateError("任务已进入提交流程，请查看任务详情；不能修改或重试。")
                recommendation_id = data.pop("recommendation_id", None)
                updated = smart.finalize(prepared=entry.prepared,
                    user_values=user_patch(data, entry.prepared, profiles), recommendation_id=recommendation_id)
                prepared_store.replace(entry, updated)
                return updated.id
            if data:
                raise FormError("确认信息包含不支持的字段，请重新打开提交前确认页面。")
            with JobRepository(config.database_path) as repository:
                lifecycle = SubmissionService(repository=repository, slurm_client=client,
                    profiles=profiles, submission_root=config.runs_root)
                # Freeze edits even on an ambiguous persistence/submit error.
                # Validation/path failures happen before effects and can be corrected.
                entry.attempted = True
                try:
                    return smart.confirm(prepared=entry.prepared, submission_service=lifecycle)
                except (PreparationError, ProjectChangedError, ValidationError, JobSpecValidationError):
                    entry.attempted = False
                    raise

    def smart_page(request, prepared, attempted=False, *, errors=(), status_code=200, posted=None):
        template = "partials/smart_prepare_result.html" if is_partial(request) else "smart.html"
        facts = catalog_details(prepared, catalog)
        context = page_context(prepared, profiles, attempted, posted, catalog)
        return page(request, template, status_code=status_code, errors=errors,
                    catalog_details=facts, display_values=compact_review_values(prepared, context["review_values"], facts),
                    file_tree=build_file_tree(prepared.project_evidence),
                    **context)

    @app.post("/new/prepare")
    async def prepare_smart_job(request: Request):
        posted = await form_data(request)
        # Each explicit analysis action gets a fresh ID, never a browser/job ID.
        with request_scope(fresh=True) as trace:
            started = time.monotonic()
            outcome = None
            selected = None
            try:
                if (set(posted) - {"folder_path", "project_dir", "task_intent", "memory_mode", "walltime_mode", "memory_mib", "time_limit_seconds"}
                        or not posted.get("task_intent", "").strip()):
                    raise AnalysisInputError("Project directory and task description required.")
                resource_choices = {}
                for key, mode_key in (("memory_mib", "memory_mode"), ("time_limit_seconds", "walltime_mode")):
                    if mode_key in posted:
                        resource_choices[mode_key] = posted[mode_key]
                        if posted[mode_key] == "explicit" and posted.get(key):
                            try:
                                resource_choices[key] = _integer(posted, key)
                            except FormError as exc:
                                raise AnalysisInputError(str(exc)) from exc
                    elif key in posted:
                        raise AnalysisInputError("Choose a resource policy before providing its value.")
                resource_choices = PreparationValues.model_validate(resource_choices)
                try:
                    if posted.get("folder_path"):
                        if posted.get("project_dir"):
                            raise FolderAccessError()
                        selected = await run_in_threadpool(folders.select, posted["folder_path"])
                    else:
                        selected = await run_in_threadpool(folders.from_absolute, posted.get("project_dir", ""))
                except FolderAccessError:
                    raise AnalysisInputError("Choose an accessible folder within the workspace.") from None
                request_smart = smart
                if transport_mode in {
                        "local_user_provider", "per_user_ssh_egress", "structured_ssh_egress"
                } and project_analyzer is None:
                    request_smart = SmartJobService(
                        analyzer=analyzer_for_request(request), cluster_service=cluster,
                        profiles=profiles, scanner=scanner, recommender=advisor, catalog=catalog,
                    )
                prepared = await run_in_threadpool(request_smart.prepare, project_dir=selected.absolute_path,
                                                   task_intent=posted["task_intent"], user_values=resource_choices)
                # An explicit working-folder choice outranks an AI subdirectory.
                # Use the existing finalization contract; no rescan/model/query.
                if "folder_path" in posted and posted.get("folder_path") and prepared.values.work_dir != str(selected.absolute_path):
                    prepared = await run_in_threadpool(request_smart.finalize, prepared=prepared,
                        user_values=PreparationValues(work_dir=str(selected.absolute_path)))
                prepared_store.add(prepared, request.session["csrf"])
                response = (smart_page(request, prepared) if is_partial(request) else
                            RedirectResponse(f"/new/prepared/{prepared.id}", status_code=303))
            except Exception as exc:
                # Prepare has no submit effects. All failures use allowlisted
                # presentation metadata, never exception text or traceback.
                failure = present_failure(exc)
                outcome = exc
                response = new_page(request, {**DEFAULT_FORM, **{k: v for k, v in posted.items() if k in DEFAULT_FORM}},
                    status_code=failure.http_status, task_intent=posted.get("task_intent", ""),
                    prepare_failure=failure, prepare_trace=trace, selected_folder=selected)
            emit("web", status="failure" if outcome is not None else "success", exc=outcome,
                 duration_ms=(time.monotonic() - started) * 1000)
            response.headers["X-Prepare-Request-ID"] = trace.request_id
            return response

    @app.get("/new/prepared/{record_id}")
    async def prepared_detail(request: Request, record_id: str):
        check_id(record_id)
        prepared, attempted = await run_in_threadpool(prepared_operation, record_id, request.session.get("csrf"))
        return smart_page(request, prepared, attempted)

    @app.post("/new/prepared/{record_id}/continue")
    async def finalize_smart_job(request: Request, record_id: str):
        check_id(record_id)
        posted = await form_data(request)
        try:
            await run_in_threadpool(prepared_operation, record_id, request.session["csrf"], "finalize", posted=posted)
        except (FormError, PreparationError, ProjectChangedError, ValidationError, JobSpecValidationError) as exc:
            prepared, attempted = await run_in_threadpool(prepared_operation, record_id, request.session["csrf"])
            messages = validation_messages(exc) if isinstance(exc, ValidationError) else list(exc.issues) if isinstance(exc, JobSpecValidationError) else [str(exc)]
            return smart_page(request, prepared, attempted, errors=messages, status_code=400,
                              posted={k: v for k, v in posted.items() if k not in {"csrf_token", "revision", "recommendation_id"}})
        if is_partial(request):
            prepared, attempted = await run_in_threadpool(prepared_operation, record_id, request.session["csrf"])
            return smart_page(request, prepared, attempted)
        return RedirectResponse(f"/new/prepared/{record_id}", status_code=303)

    @app.post("/new/prepared/{record_id}/confirm")
    async def confirm_smart_job(request: Request, record_id: str):
        check_id(record_id)
        posted = await form_data(request)
        try:
            record = await run_in_threadpool(prepared_operation, record_id, request.session["csrf"], "confirm", posted=posted)
        except SubmissionServiceError as exc:
            request.session["notice"] = "提交未明确完成，请查看持久化状态。当前系统不会自动重新提交。"
            return RedirectResponse(f"/jobs/{exc.record_id}", status_code=303)
        except (FormError, PreparationError, ProjectChangedError, ValidationError, JobSpecValidationError) as exc:
            prepared, attempted = await run_in_threadpool(prepared_operation, record_id, request.session["csrf"])
            messages = validation_messages(exc) if isinstance(exc, ValidationError) else list(exc.issues) if isinstance(exc, JobSpecValidationError) else [str(exc)]
            return smart_page(request, prepared, attempted, errors=messages, status_code=400)
        return RedirectResponse(f"/jobs/{record.id}", status_code=303)

    @app.get("/cluster")
    async def cluster_dashboard(request: Request):
        try:
            snapshot = await run_in_threadpool(cluster.get_snapshot)
        except ClusterUnavailableError as exc:
            # Do not log nested CommandResult/stdout, which can contain the
            # transient queue user roster. The service exposes a safe summary.
            logger.warning("Cluster snapshot unavailable: %s", exc)
            return page(request, "cluster.html", status_code=503, snapshot=None)
        return page(request, "cluster.html", snapshot=snapshot)

    @app.post("/new")
    async def create_job(request: Request):
        form = await form_data(request)
        unknown = set(form) - DEFAULT_FORM.keys()
        if unknown:
            return new_page(request, {**DEFAULT_FORM, **form}, ["表单含有不支持的字段。"], 400)
        try:
            form = apply_catalog_to_form(form, catalog)
            spec = spec_from_form(form, profiles, recommendations=manual_resource_recommendations(form, profiles, catalog))
            record = await run_in_threadpool(operation, "create_job", spec=spec, name=form["name"])
        except (FormError, ValidationError, JobSpecValidationError) as exc:
            errors = (validation_messages(exc) if isinstance(exc, ValidationError) else
                      list(exc.issues) if isinstance(exc, JobSpecValidationError) else [str(exc)])
            return new_page(request, {**DEFAULT_FORM, **form}, errors, 400)
        return RedirectResponse(f"/jobs/{record.id}", status_code=303)

    @app.post("/new/catalog")
    async def apply_catalog(request: Request):
        posted = await form_data(request)
        form = {**DEFAULT_FORM, **posted}
        try:
            if set(posted) - DEFAULT_FORM.keys():
                raise FormError("表单含有不支持的字段。")
            form = apply_catalog_to_form(form, catalog, explicit=True)
            entry = catalog.software_by_id(form["software_id"]) if catalog else None
            return new_page(request, form, notice=provenance(entry) if entry else "请选择 Catalog 软件。")
        except FormError as exc:
            return new_page(request, form, [str(exc)], 400)

    @app.post("/new/resource-policies")
    async def check_resource_policies(request: Request):
        posted = await form_data(request)
        if set(posted) - DEFAULT_FORM.keys():
            raise HTTPException(400, "表单含有不支持的字段。")
        form = {**DEFAULT_FORM, **posted}
        if not is_partial(request):
            return new_page(request, form)
        return page(request, "partials/manual_resource_policies.html", form=display_form(form),
                    policy_recommendations=manual_resource_recommendations(form, profiles, catalog))

    @app.post("/new/recommend")
    async def recommend_resources(request: Request):
        posted = await form_data(request)
        form = {**DEFAULT_FORM, **posted}
        if set(posted) - DEFAULT_FORM.keys():
            return new_page(request, form, ["表单含有不支持的字段。"], 400)
        try:
            form = apply_catalog_to_form(form, catalog)
            advisory_request = recommendation_request_from_form(form, profiles,
                recommendations=manual_resource_recommendations(form, profiles, catalog))
            if catalog:
                advisory_request.catalog_compatibility = compatibility(
                    catalog.software_by_id(form.get("software_id")),
                    catalog.environment(advisory_request.environment_profile))
            preference = UserPreference(form["preference"])
        except (FormError, ValidationError, ValueError) as exc:
            messages = validation_messages(exc) if isinstance(exc, ValidationError) else [str(exc)]
            return new_page(request, form, messages, 400)
        try:
            snapshot = await run_in_threadpool(cluster.get_snapshot)
            report = await run_in_threadpool(advisor.recommend, spec=advisory_request, snapshot=snapshot,
                                             profiles=profiles, preference=preference, as_of=datetime.now(timezone.utc),
                                             fixed_memory="memory_mode" in posted)
        except RecommendationInputError as exc:
            return new_page(request, form, [str(exc)], 400)
        except Exception as exc:
            # Advice is optional; no record or stale snapshot is used on failure.
            logger.warning("Resource advice unavailable: %s", type(exc).__name__)
            return new_page(request, form, ["资源推荐暂不可用。仍可手动填写 partition/resources 并创建任务。"], 503)
        tokens = {item.id: choices.dumps({"form": form, "resources": item.proposed_resources.model_dump(mode="json"),
                                         "csrf": request.session["csrf"]})
                  for item in report.recommendations[:3]}
        return new_page(request, form, report=report, apply_tokens=tokens)

    @app.post("/new/scan")
    async def scan_project(request: Request):
        posted = await form_data(request)
        form = {**DEFAULT_FORM, **posted}
        if set(posted) - DEFAULT_FORM.keys():
            return new_page(request, form, ["表单含有不支持的字段。"], 400)
        try:
            evidence = await run_in_threadpool(scanner.scan, form["project_dir"])
        except ProjectScanError as exc:
            return new_page(request, form, [str(exc)], 400)
        except Exception as exc:
            logger.error("Project scan failed", exc_info=exc)
            return new_page(request, form, ["项目扫描暂不可用；仍可手动填写任务表单。"], 503)
        # Independent evidence preview. No form mutation, JobSpec construction,
        # repository access, recommendation, cluster query or lifecycle call.
        # Signed, session-bound evidence from this scan; no filesystem rescan or
        # server analysis history on Analyze. Keep only context-selected items.
        try:
            context = AnalysisContextBuilder().build(evidence, "分析已扫描项目的运行方式")
        except AnalysisInputError:
            # AI preview limits must not break the independent Scanner/manual UI.
            return new_page(request, form, project_evidence=evidence)
        ids = set(context.evidence_refs)
        subset = evidence.model_dump(mode="json")
        subset["evidence_items"] = [e.model_dump(mode="json") for e in evidence.evidence_items if e.id in ids]
        for group in GROUPS:
            subset[group] = [{**c.model_dump(mode="json"), "evidence_ids": sorted(ids & set(c.evidence_ids))}
                             for c in getattr(evidence, group) if ids & set(c.evidence_ids)]
        subset["warnings"] = sorted(set(subset["warnings"]) | set(context.warnings))
        scan_token = scans.dumps({"evidence": subset, "form": form, "csrf": request.session["csrf"]})
        if len(scan_token.encode("utf-8")) > MAX_FORM_BYTES // 2:
            scan_token = None
        return new_page(request, form, project_evidence=evidence, scan_token=scan_token)

    @app.post("/new/analyze")
    async def analyze_project(request: Request):
        posted = await form_data(request)
        if set(posted) != {"scan_token", "task_intent"}:
            raise HTTPException(400, "分析请求不合法，请先 Scan Project。")
        try:
            payload = scans.loads(posted["scan_token"], max_age=APPLY_MAX_AGE_SECONDS)
            if payload["csrf"] != request.session["csrf"]:
                raise ValueError
            evidence = ProjectEvidence.model_validate(payload["evidence"])
            form = payload["form"]
        except (BadSignature, ValueError, KeyError, TypeError):
            raise HTTPException(400, "扫描证据已失效或被修改，请重新 Scan Project；仍可手动填写。") from None
        result = None
        errors, status = [], 200
        try:
            request_analyzer = analyzer_for_request(request)
            result = await run_in_threadpool(
                request_analyzer.analyze, evidence=evidence,
                task_intent=posted["task_intent"],
            )
        except AnalysisInputError as exc:
            errors, status = [str(exc)], 400
        except ModelUnavailableError as exc:
            logger.warning("AI analysis failed: code=%s http_status=%s", exc.code.value, exc.http_status)
            errors, status = ["AI 分析暂不可用，仍可手动配置。",
                              prepare_failure_copy(present_failure(exc).code)[1]], 503
        except AnalysisOutputValidationError as exc:
            diagnostic = exc.safe_diagnostic()
            logger.warning("AI analysis failed: code=invalid_structured_response %s", diagnostic)
            errors, status = ["AI 分析暂不可用，仍可手动配置。",
                              "AI 结果未通过格式或依据校验，请手动核对；不会自动重试。",
                              "校验位置：" + diagnostic], 503
        except Exception as exc:
            # Never log provider exceptions/bodies, model output, intent or key.
            logger.warning("AI analysis failed: %s", type(exc).__name__)
            errors, status = ["AI 分析暂不可用，仍可手动创建任务。"], 503
        return new_page(request, form, errors, status, project_evidence=evidence,
                        scan_token=posted["scan_token"], analysis=result, task_intent=posted["task_intent"])

    @app.post("/new/apply")
    async def apply_recommendation(request: Request):
        posted = await form_data(request)
        if set(posted) != {"choice"}:
            raise HTTPException(400, "推荐选项不合法，请重新获取推荐。")
        try:
            payload = choices.loads(posted["choice"], max_age=APPLY_MAX_AGE_SECONDS)
            if payload["csrf"] != request.session["csrf"]:
                raise ValueError("different session")
            resources = Resources.model_validate(payload["resources"])
            form = apply_resources_to_form(payload["form"], resources)
        except (BadSignature, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, "推荐已失效或被修改，请重新获取推荐；也可手工填写资源。") from exc
        # Only form state changes. No query, render, create, submit or SQL here.
        return new_page(request, form, notice="已填入建议资源；可继续修改。请 Create 后检查脚本，再决定是否 Submit。")

    @app.get("/jobs")
    async def jobs(request: Request):
        records = await run_in_threadpool(operation, "list")
        notice = request.session.pop("notice", None)
        message = None
        if notice:
            message = {
                "refreshed": "已保存本次查询结果。",
                "unsubmitted": "尚无已提交的 Slurm job，无法刷新状态。",
                "refresh_error": "状态查询失败，保留最近一次结果。请检查服务端日志。",
            }.get(notice[1])
        return page(request, "jobs.html", records=records, message=message)

    @app.get("/jobs/{record_id}")
    async def detail(request: Request, record_id: str):
        check_id(record_id)
        record = await run_in_threadpool(operation, "get", record_id)
        # Fixed notice codes only, bound to a record. No raw exceptions/captures
        # stored in the signed (readable) cookie.
        notice = request.session.pop("notice", None)
        message = None
        if notice and notice[0] == record_id:
            message = {
                "submitted": "提交回执已保存。状态尚需手动刷新。",
                "refreshed": "已保存本次查询结果。",
                "blocked": "此记录不能再次提交。请核对当前状态与提交目录配置。",
                "unsubmitted": "尚无已提交的 Slurm job，无法刷新状态。",
                "submit_error": "提交未能确认成功，请查看下方提交状态并核对 Slurm；系统不会自动重试。",
                "refresh_error": "状态查询失败，保留最近一次结果。请检查服务端日志。",
            }.get(notice[1])
        return page(request, "detail.html", record=record, message=message)

    async def action(request, record_id, method):
        posted = await form_data(request)
        return_to = posted.pop("return_to", None)
        if posted or return_to not in {None, "jobs"}:
            raise HTTPException(400, "任务操作请求无效。")
        check_id(record_id)
        try:
            await run_in_threadpool(operation, method, record_id)
            notice = "submitted" if method == "submit_job" else "refreshed"
        except JobNotSubmittableError:
            notice = "blocked"
        except JobNotSubmittedError:
            notice = "unsubmitted"
        except SubmissionServiceError as exc:
            logger.error("Lifecycle action failed for %s", record_id, exc_info=exc)
            notice = "submit_error" if method == "submit_job" else "refresh_error"
        request.session["notice"] = [record_id, notice]
        if method == "refresh_status" and is_partial(request):
            record = await run_in_threadpool(operation, "get", record_id)
            request.session.pop("notice", None)
            message = {"refreshed": "已保存本次查询结果。",
                       "unsubmitted": "尚无已提交的 Slurm job，无法刷新状态。",
                       "refresh_error": "状态查询失败，保留最近一次结果。请检查服务端日志。"}.get(notice)
            return page(request, "partials/job_status.html", record=record, message=message,
                        status_code=503 if notice == "refresh_error" else 200)
        if method == "refresh_status" and return_to == "jobs":
            return RedirectResponse("/jobs", status_code=303)
        return RedirectResponse(f"/jobs/{record_id}", status_code=303)

    @app.post("/jobs/{record_id}/submit")
    async def submit(request: Request, record_id: str):
        return await action(request, record_id, "submit_job")

    @app.post("/jobs/{record_id}/refresh")
    async def refresh(request: Request, record_id: str):
        return await action(request, record_id, "refresh_status")

    # Host validation wraps cookie/session handling. Session lookup then runs
    # before the authentication and transitional legacy-route boundary.
    app.add_middleware(AuthenticationBoundaryMiddleware,
                       enabled=config.authentication_enabled,
                       process_username=process_identity,
                       bootstrap_enabled=config.deployment_mode == "ssh_first")
    app.add_middleware(ServerSessionMiddleware, manager=sessions,
                       secure=config.session_cookie_secure,
                       cookie_name=config.session_cookie_name)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.trusted_hosts),
                       www_redirect=False)

    return app
