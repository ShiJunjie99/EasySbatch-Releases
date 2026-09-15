"""Build a native Beta EasySbatch installer on its target operating system."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from prepare_beta_desktop import DEFAULT_OUTPUT, prepare


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "beta-easysbatch"


def _check_host(target: str) -> None:
    machine = platform.machine().lower()
    if target == "win-x64" and (sys.platform != "win32" or machine not in {"amd64", "x86_64"}):
        raise RuntimeError("win-x64 must be built on a Windows x64 host")
    if target == "mac-arm64" and (sys.platform != "darwin" or machine not in {"arm64", "aarch64"}):
        raise RuntimeError("mac-arm64 must be built on an Apple Silicon macOS host")


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def _build_core(target: str) -> Path:
    core_dist = BUILD_ROOT / "core-dist" / target
    core_work = BUILD_ROOT / "core-work" / target
    shutil.rmtree(core_dist, ignore_errors=True)
    shutil.rmtree(core_work, ignore_errors=True)
    environment = os.environ.copy()
    environment["BETA_EASYSBATCH_CORE_NAME"] = "BetaEasySbatchCore"
    _run([
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(core_dist),
        "--workpath",
        str(core_work),
        "packaging/beta_easysbatch_core.spec",
    ], cwd=ROOT, env=environment)
    binary = core_dist / ("BetaEasySbatchCore.exe" if target == "win-x64" else "BetaEasySbatchCore")
    if not binary.is_file():
        raise RuntimeError(f"PyInstaller did not produce {binary}")
    _run([str(binary), "--self-test"], cwd=core_dist)
    return binary.resolve()


def build(target: str, *, source: Path | None, signed: bool) -> Path:
    _check_host(target)
    dsh = prepare(DEFAULT_OUTPUT, source=source)
    core = _build_core(target)
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise RuntimeError("pnpm 11.7.0 is required")
    environment = os.environ.copy()
    environment.update({
        "BETA_EASYSBATCH_CORE_BINARY": str(core),
        "BETA_EASYSBATCH_UNSIGNED": "0" if signed else "1",
        "DSH_CLIENT_TITLE": "Beta EasySbatch",
        "DSH_DESKTOP_APP_ID": "com.easysbatch.beta",
        "DSH_DESKTOP_AUTO_UPDATE_ENV": "test",
        "DOWNLOAD_TEST_ORIGIN": "https://updates.invalid",
        "CI": "true",
    })
    _run([pnpm, "install", "--no-frozen-lockfile"], cwd=dsh, env=environment)
    script = {
        "win-x64": "package:desktop:win:x64",
        "mac-arm64": "package:desktop:mac:arm64",
    }[target]
    _run([pnpm, "run", script], cwd=dsh, env=environment)
    upstream_artifacts = dsh / "apps" / "desktop" / ".desktop-build" / "targets" / target / "artifacts"
    if not upstream_artifacts.is_dir():
        raise RuntimeError(f"Desktop build produced no artifact directory: {upstream_artifacts}")
    destination = ROOT / "dist" / "beta-easysbatch" / target
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(upstream_artifacts, destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=["win-x64", "mac-arm64"])
    parser.add_argument("--source", type=Path, help="exact pinned local DSH checkout; network-free")
    parser.add_argument(
        "--signed",
        action="store_true",
        help="require the upstream platform signing environment; the default is an unsigned test build",
    )
    args = parser.parse_args(argv)
    result = build(args.target, source=args.source, signed=args.signed)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
