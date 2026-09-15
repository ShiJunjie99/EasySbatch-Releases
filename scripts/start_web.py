"""Load private local TOML into the backend environment and run loopback Web.

No shell evaluation, dotenv interpolation, system configuration, or auto API call.
--check-ai explicitly runs only the existing tiny (billable) provider probe.
--restart stops only the verified local preview identified by its PID file.
"""

import argparse
import os
from pathlib import Path
import re
import signal
import socket
import stat
import time
import tomllib
from urllib.parse import urlsplit

from sbatch_agent.local_ai_protocol import MODEL, PROVIDER
from sbatch_agent.local_user_model_client import LocalUserProviderConfig
from sbatch_agent.model_client import ModelConfig, OpenAICompatibleClient
from sbatch_agent.cluster_profile import ClusterProfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / ".sbatch-agent/deepseek.toml"
CONFIG_LIMIT = 16 * 1024


class StartupError(ValueError):
    """User-facing message with no raw config, paths from config or secrets."""


def landing_path():
    return "/login" if os.environ.get("SBATCH_AGENT_WEB_AUTH_ENABLED") == "1" else "/new"


def enable_ssh_first(environment):
    """Select the reversible Alpha mode without rewriting private TOML."""
    result = dict(environment)
    result.update({
        "SBATCH_AGENT_WEB_AUTH_ENABLED": "1",
        "SBATCH_AGENT_SESSION_COOKIE_SECURE": "0",
        "SBATCH_AGENT_DEPLOYMENT_MODE": "ssh_first",
        "SBATCH_AGENT_PUBLIC_BASE_URL": "",
        "SBATCH_AGENT_WORKER_BROKER_SOCKET": (
            f"/tmp/easysbatch-{os.geteuid()}/broker.sock"
        ),
    })
    return result


def load_settings(path: Path, root: Path) -> tuple[dict[str, str], int]:
    try:
        # A private config is explicit and bounded, never a project shell file.
        with path.open("rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise StartupError("配置必须是普通文本文件。")
            if mode & 0o077:
                raise StartupError("配置文件权限应为 600；请执行 chmod 600 后再启动。")
            raw = stream.read(CONFIG_LIMIT + 1)
        if len(raw) > CONFIG_LIMIT:
            raise StartupError("配置文件过大。")
        data = tomllib.loads(raw.decode("utf-8"))
        if set(data) != {"ai", "web"}:
            raise ValueError
        ai, web = data["ai"], data["web"]
        required_ai = {"provider", "model", "endpoint", "timeout"}
        if not required_ai <= set(ai) or set(ai) - required_ai - {"api_key", "ca_bundle", "transport_mode"}:
            raise ValueError
        required_web = {"port", "database_path", "runs_root", "profiles_path"}
        optional_web = {
            "catalog_path", "workspace_root", "max_entries_per_directory",
            "authentication_enabled", "ssh_host", "ssh_port", "ssh_known_hosts",
            "session_idle_timeout_seconds", "session_cookie_secure",
            "deployment_mode", "public_base_url", "worker_broker_socket",
        }
        if not required_web <= set(web) or set(web) - required_web - optional_web:
            raise ValueError
        if any(not isinstance(ai[k], str) for k in ("provider", "model", "endpoint")):
            raise ValueError
        transport_mode = ai.get("transport_mode", "direct")
        if transport_mode not in {
                "direct", "legacy_relay", "per_user_ssh_egress",
                "structured_ssh_egress", "local_user_provider"}:
            raise ValueError
        if transport_mode == "local_user_provider":
            # B7 server startup deliberately has no api_key field/env.  The
            # fixed endpoint/model are trusted application configuration, not
            # browser-controlled values; the key is configured in Launcher.
            if "api_key" in ai or ai["provider"] != PROVIDER or ai["model"] != MODEL:
                raise ValueError
            try:
                local_config = LocalUserProviderConfig(
                    provider=ai["provider"], model=ai["model"],
                    endpoint=ai["endpoint"], timeout=float(ai["timeout"]),
                )
            except (TypeError, ValueError):
                raise ValueError from None
            values = {
                "SBATCH_AGENT_AI_PROVIDER": local_config.provider,
                "SBATCH_AGENT_AI_MODEL": local_config.model,
                "SBATCH_AGENT_AI_ENDPOINT": local_config.endpoint,
                "SBATCH_AGENT_AI_TIMEOUT": str(local_config.timeout),
            }
        else:
            if "api_key" not in ai or not isinstance(ai["api_key"], str):
                raise ValueError
            config = ModelConfig(ai["provider"], ai["model"], ai["endpoint"],
                                 "SBATCH_AGENT_LOCAL_AI_KEY", ai["timeout"])
            values = {
                "SBATCH_AGENT_AI_PROVIDER": config.provider, "SBATCH_AGENT_AI_MODEL": config.model,
                "SBATCH_AGENT_AI_ENDPOINT": config.endpoint, "SBATCH_AGENT_AI_API_KEY_ENV": config.api_key_env,
                "SBATCH_AGENT_AI_TIMEOUT": str(config.timeout), config.api_key_env: ai["api_key"],
            }
        port = web["port"]
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError
        values["SBATCH_AGENT_AI_TRANSPORT_MODE"] = transport_mode
        ca_bundle = ai.get("ca_bundle", "")
        if not isinstance(ca_bundle, str):
            raise ValueError
        if ca_bundle:
            ca_path = (root / ca_bundle).absolute()
            if not ca_path.is_file():
                raise StartupError("ca_bundle 指定的证书文件不存在；请填写系统已有的 CA 证书包路径。")
            # Standard OpenSSL setting, local to the new backend process.
            # Never download trust roots or disable certificate/hostname checks.
            values["SSL_CERT_FILE"] = str(ca_path)
        for field, env_name in (("database_path", "DATABASE_PATH"), ("runs_root", "RUNS_ROOT"),
                                ("profiles_path", "PROFILES_PATH")):
            value = web[field]
            if not isinstance(value, str) or (not value and field != "profiles_path"):
                raise ValueError
            # Resolve relative to repository, independent of the terminal cwd.
            values["SBATCH_AGENT_" + env_name] = str((root / value).absolute()) if value else ""
        profile_path = values["SBATCH_AGENT_PROFILES_PATH"]
        if "workspace_root" in web:
            if not isinstance(web["workspace_root"], str) or not web["workspace_root"]:
                raise ValueError
            values["SBATCH_AGENT_WORKSPACE_ROOT"] = str((root / web["workspace_root"]).absolute())
        if "max_entries_per_directory" in web:
            limit = web["max_entries_per_directory"]
            if type(limit) is not int or not 1 <= limit <= 500:
                raise ValueError
            values["SBATCH_AGENT_FOLDER_MAX_ENTRIES"] = str(limit)
        if "catalog_path" in web:
            if not isinstance(web["catalog_path"], str):
                raise ValueError
            values["SBATCH_AGENT_SERVER_CATALOG_PATH"] = str((root / web["catalog_path"]).absolute()) if web["catalog_path"] else ""
        auth_enabled = web.get("authentication_enabled", False)
        cookie_secure = web.get("session_cookie_secure", False)
        ssh_host = web.get("ssh_host", "cluster.example.edu")
        ssh_port = web.get("ssh_port", 22)
        idle = web.get("session_idle_timeout_seconds", 1800)
        deployment_mode = web.get("deployment_mode", "loopback_legacy")
        public_base_url = web.get("public_base_url", "")
        worker_broker_socket = web.get("worker_broker_socket", "")
        ClusterProfile("configured-cluster", "Configured cluster", ssh_host, ssh_port)
        if (type(auth_enabled) is not bool or type(cookie_secure) is not bool or
                type(idle) is not int or not 60 <= idle <= 86400 or
                deployment_mode not in {"loopback_legacy", "ssh_first", "lan_https"} or
                not isinstance(public_base_url, str) or
                not isinstance(worker_broker_socket, str)):
            raise ValueError
        if deployment_mode == "lan_https":
            try:
                public_url = urlsplit(public_base_url)
                public_port = public_url.port
            except ValueError:
                raise ValueError from None
            if (not auth_enabled or not cookie_secure or public_url.scheme != "https" or
                    public_url.hostname != ssh_host or public_port is None or
                    not 1024 <= public_port <= 65535 or public_url.path not in {"", "/"} or
                    public_url.query or public_url.fragment or public_url.username is not None or
                    public_url.password is not None):
                raise ValueError
            if worker_broker_socket:
                raise ValueError
        elif deployment_mode == "ssh_first":
            if (not auth_enabled or cookie_secure or public_base_url or
                    re.fullmatch(
                        r"/tmp/easysbatch-[0-9]+/broker\.sock",
                        worker_broker_socket,
                    ) is None):
                raise ValueError
        elif public_base_url or worker_broker_socket:
            raise ValueError
        values.update({
            "SBATCH_AGENT_WEB_AUTH_ENABLED": "1" if auth_enabled else "0",
            "SBATCH_AGENT_SSH_HOST": ssh_host,
            "SBATCH_AGENT_SSH_PORT": str(ssh_port),
            "SBATCH_AGENT_SESSION_IDLE_SECONDS": str(idle),
            "SBATCH_AGENT_SESSION_COOKIE_SECURE": "1" if cookie_secure else "0",
            "SBATCH_AGENT_DEPLOYMENT_MODE": deployment_mode,
            "SBATCH_AGENT_PUBLIC_BASE_URL": public_base_url,
            "SBATCH_AGENT_WORKER_BROKER_SOCKET": worker_broker_socket,
        })
        if "ssh_known_hosts" in web:
            known_hosts = web["ssh_known_hosts"]
            if not isinstance(known_hosts, str) or not known_hosts:
                raise ValueError
            known_hosts_path = Path(known_hosts)
            if not known_hosts_path.is_absolute():
                known_hosts_path = (root / known_hosts_path).absolute()
            if not known_hosts_path.is_file():
                raise StartupError("ssh_known_hosts 指定的主机密钥文件不存在。")
            values["SBATCH_AGENT_SSH_KNOWN_HOSTS"] = str(known_hosts_path)
        if profile_path and not Path(profile_path).is_file():
            raise StartupError("profiles_path 指定的文件不存在；请使用已登记的 profiles 路径。")
        return values, port
    except FileNotFoundError:
        raise StartupError("本地配置不存在；请先复制 config/deepseek.example.toml 到 .sbatch-agent/deepseek.toml。") from None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, StartupError):
            raise
        # TOML decoding errors may echo a secret; never include their text.
        raise StartupError("无法读取配置或配置格式错误；请对照模板检查，保留字符串引号。") from None


def restart_preview(pid_file: Path, root: Path, port: int):
    """Linux local development only; never kill by port/name or use SIGKILL."""
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
        if pid <= 1 or pid == os.getpid():
            raise ValueError
        proc = Path("/proc") / str(pid)
        if not proc.exists():
            return
        args = (proc / "cmdline").read_bytes().split(b"\0")
        own_script = any(arg in {b"scripts/start_web.py", str(root / "scripts/start_web.py").encode()} for arg in args[1:])
        old_preview = (b"sbatch_agent.web:create_app" in args and b"--factory" in args
                       and b"127.0.0.1" in args and str(port).encode() in args)
        if (proc.stat().st_uid != os.geteuid() or (proc / "cwd").resolve() != root
                or not (own_script or old_preview)):
            raise ValueError
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                dead = (proc / "stat").read_text().split(") ", 1)[1].startswith("Z")
            except FileNotFoundError:
                return
            if dead:
                return
            time.sleep(0.1)
    except ProcessLookupError:
        return
    except (OSError, ValueError, IndexError):
        raise StartupError("无法核实旧 Web 进程，未执行强制停止；请在原启动终端按 Ctrl+C。") from None
    raise StartupError("旧 Web 尚未退出；未强制停止，也未启动第二个服务。")


def serve(root: Path, port: int, restart: bool):
    import uvicorn

    pid_file = root / ".sbatch-agent" / ("web.pid" if port == 8000 else f"web-{port}.pid")
    if pid_file.is_symlink():
        raise StartupError("PID 文件不能是符号链接。")
    if restart:
        restart_preview(pid_file, root, port)
    # Reserve the port before writing PID or opening a DB. Do not overwrite a
    # different listener, and do not let a failed second start lose the old PID.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise StartupError("本机端口无法使用；如是此前的本项目 Web，可使用 --restart。") from None
        pid_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if pid_file.is_symlink():
            raise StartupError("PID 文件不能是符号链接。")
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        try:
            lan_https = os.environ.get("SBATCH_AGENT_DEPLOYMENT_MODE") == "lan_https"
            if os.environ.get("SBATCH_AGENT_DEPLOYMENT_MODE") == "ssh_first":
                print("SSH-first 服务已就绪；请在用户电脑运行 easysbatch-launcher。"
                      "\n关闭服务：在此终端按 Ctrl+C。", flush=True)
            else:
                displayed_url = (os.environ.get("SBATCH_AGENT_PUBLIC_BASE_URL", "").rstrip("/")
                                 if lan_https else f"http://127.0.0.1:{port}")
                print(f"网页：{displayed_url}{landing_path()}\n关闭服务：在此终端按 Ctrl+C。",
                      flush=True)
            uvicorn.Server(uvicorn.Config(
                "sbatch_agent.web:create_app", factory=True, host="127.0.0.1", port=port,
                proxy_headers=lan_https,
                forwarded_allow_ips="127.0.0.1" if lan_https else "",
            )).run(sockets=[listener])
        finally:
            if pid_file.exists() and pid_file.read_text(encoding="ascii") == str(os.getpid()):
                pid_file.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-ai", action="store_true", help="一次最小真实 API 请求，不扫描项目、不启动 Web。")
    mode.add_argument("--restart", action="store_true", help="核实并停止此前的本项目 Web，再以前台方式启动。")
    parser.add_argument("--ssh-first", action="store_true",
                        help="使用 SSH-first Alpha；不改写私有配置或启动 LAN proxy。")
    args = parser.parse_args(argv)
    try:
        environment, port = load_settings(args.config, ROOT)
        if args.ssh_first:
            if args.check_ai:
                raise StartupError("AI 单次检查不能同时启动 SSH-first Web。")
            environment = enable_ssh_first(environment)
        if environment.get("SBATCH_AGENT_AI_TRANSPORT_MODE") == "local_user_provider":
            # Do not let a stale legacy environment variable accidentally put
            # a server-side key into the new process.  The local mode ignores
            # it functionally, but removing it also makes the boundary
            # inspectable in deployment diagnostics.
            old_key_env = os.environ.get("SBATCH_AGENT_AI_API_KEY_ENV")
            for name in {old_key_env, "SBATCH_AGENT_LOCAL_AI_KEY", "DEEPSEEK_API_KEY"}:
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    os.environ.pop(name, None)
            os.environ.pop("SBATCH_AGENT_AI_API_KEY_ENV", None)
        os.environ.update(environment)
        os.chdir(ROOT)
        if args.check_ai:
            if os.environ.get("SBATCH_AGENT_AI_TRANSPORT_MODE") == "local_user_provider":
                print("local_user_provider 的 AI 测试必须在当前用户 Launcher 上执行；服务器不访问 DeepSeek。")
                return 2
            from smoke_ai_provider import main as smoke_main
            return smoke_main([])
        if os.environ.get("SBATCH_AGENT_AI_TRANSPORT_MODE") != "local_user_provider":
            client = OpenAICompatibleClient(ModelConfig.from_env())
            if client.availability().state != "available":
                print("AI key 尚未填写；网页仍可使用 Scan Project 和手工表单。")
        serve(ROOT, port, args.restart)
        return 0
    except StartupError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
