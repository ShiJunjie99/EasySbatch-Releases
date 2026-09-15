"""Fixed M10-B3 kernel peer-identity smoke; never reads SSH credentials."""

import json
import os
from pathlib import Path
import subprocess
import sys

from sbatch_agent import user_worker
from sbatch_agent.worker_broker import WorkerBroker, WorkerBrokerError


def main():
    uid = os.geteuid()
    if uid <= 0:
        print("SO_PEERCRED smoke refuses root.")
        return 2
    # Use an isolated numeric runtime directory so the smoke can run beside the
    # live Central Broker without touching its socket or registered Workers.
    socket_path = f"/tmp/easysbatch-{uid}{os.getpid()}/broker.sock"
    runtime_directory = Path(socket_path).parent
    broker = WorkerBroker(socket_path)
    process = None
    context = None
    stage = "broker-start"
    try:
        broker.start()
        stage = "worker-start"
        environment = {
            "PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LC_ALL": "C",
        }
        worker_source = Path(user_worker.__file__).read_text(encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", worker_source, "--socket", socket_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, env=environment,
        )
        stage = "worker-ready"
        raw = process.stdout.readline(32770)
        prefix = user_worker.WORKER_READY_PREFIX.encode("ascii")
        if not raw.startswith(prefix):
            raise RuntimeError("missing worker ready prefix")
        report = json.loads(raw[len(prefix):].decode("utf-8"))
        stage = "bootstrap"
        token = report.pop("bootstrap_token")
        context = broker.consume_bootstrap(report["worker_id"], token)
        token = None
        stage = "identity"
        identity = context.verify_identity()
        if identity.uid != uid:
            raise RuntimeError("kernel identity mismatch")
        print(f"SO_PEERCRED PASS username={identity.username} uid={identity.uid}")
        return 0
    except Exception as exc:
        # Fixed diagnostics only: never echo protocol data, tokens or exception text.
        category = exc.code if isinstance(exc, WorkerBrokerError) else type(exc).__name__
        print(f"SO_PEERCRED FAIL stage={stage} category={category}")
        return 2
    finally:
        if context is not None:
            context.close()
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        broker.stop()
        try:
            broker.worker_entrypoint.unlink()
        except OSError:
            pass
        try:
            runtime_directory.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
