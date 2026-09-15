"""Build a native Beta EasySbatch installer on its target operating system."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from prepare_beta_desktop import DEFAULT_OUTPUT, prepare


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "beta-easysbatch"
DEFAULT_PRODUCT_VERSION = "0.2.0-beta.1"
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
VERSION_MANIFEST_GLOBS = (
    "apps/*/package.json",
    "packages/*/*/package.json",
)
RELEASE_ARTIFACT_SUFFIXES = (".exe", ".dmg", ".zip", ".blockmap", ".yml", ".json")


def _check_host(target: str) -> None:
    machine = platform.machine().lower()
    if target == "win-x64" and (sys.platform != "win32" or machine not in {"amd64", "x86_64"}):
        raise RuntimeError("win-x64 must be built on a Windows x64 host")
    if target == "mac-arm64" and (sys.platform != "darwin" or machine not in {"arm64", "aarch64"}):
        raise RuntimeError("mac-arm64 must be built on an Apple Silicon macOS host")


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def _product_version(explicit: str | None) -> str:
    value = explicit or os.environ.get("BETA_EASYSBATCH_VERSION")
    if value is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME", "")
        value = tag[1:] if tag.startswith("v") else tag
    value = value or DEFAULT_PRODUCT_VERSION
    if VERSION_PATTERN.fullmatch(value) is None:
        raise RuntimeError(f"invalid Beta EasySbatch version: {value!r}")
    return value


def _stamp_product_version(dsh: Path, version: str) -> None:
    paths = {dsh / "package.json"}
    for pattern in VERSION_MANIFEST_GLOBS:
        paths.update(dsh.glob(pattern))
    for path in sorted(paths):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["version"] = version
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _require_environment(names: tuple[str, ...], *, label: str) -> None:
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(f"{label} requires environment values: {', '.join(missing)}")


def _check_signing_environment(target: str, signed: bool) -> None:
    if not signed:
        return
    if target == "win-x64":
        if os.environ.get("BETA_EASYSBATCH_WINDOWS_SIGNING_MODE") == "safenet":
            _require_environment((
                "DSH_DESKTOP_WINDOWS_CER_FILE",
                "DSH_DESKTOP_WINDOWS_KEY_CONTAINER",
                "DSH_DESKTOP_WINDOWS_SIGNTOOL",
                "DSH_DESKTOP_WINDOWS_TOKEN_PIN",
            ), label="SafeNet Windows signing")
        else:
            _require_environment(
                ("WIN_CSC_LINK", "WIN_CSC_KEY_PASSWORD"),
                label="PFX Windows signing",
            )
        return
    _require_environment((
        "DSH_DESKTOP_MACOS_SIGNING_IDENTITY",
        "DSH_DESKTOP_MACOS_TEAM_ID",
        "APPLE_API_KEY",
        "APPLE_API_KEY_ID",
        "APPLE_API_ISSUER",
    ), label="macOS Developer ID signing and notarization")


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


def _copy_release_artifacts(source: Path, destination: Path, target: str) -> None:
    """Copy release files without duplicating Electron's unpacked build tree."""
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    copied: list[str] = []
    for path in source.iterdir():
        if not path.is_file():
            continue
        is_product_artifact = path.name.startswith("Beta-EasySbatch-")
        is_metadata = path.name in {"beta.yml", "beta-mac.yml", f"{target}-release.json"}
        if is_metadata or (is_product_artifact and path.name.endswith(RELEASE_ARTIFACT_SUFFIXES)):
            shutil.copy2(path, destination / path.name)
            copied.append(path.name)
    primary_suffix = ".exe" if target == "win-x64" else ".dmg"
    if not any(name.endswith(primary_suffix) for name in copied):
        raise RuntimeError(f"Desktop build produced no {primary_suffix} release artifact")


def build(target: str, *, source: Path | None, signed: bool, version: str | None = None) -> Path:
    _check_host(target)
    _check_signing_environment(target, signed)
    dsh = prepare(DEFAULT_OUTPUT, source=source)
    _stamp_product_version(dsh, _product_version(version))
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
    _copy_release_artifacts(upstream_artifacts, destination, target)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=["win-x64", "mac-arm64"])
    parser.add_argument("--source", type=Path, help="exact pinned local DSH checkout; network-free")
    parser.add_argument("--version", help="Beta EasySbatch semantic version; defaults from the tag or product Beta")
    parser.add_argument(
        "--signed",
        action="store_true",
        help="require the upstream platform signing environment; the default is an unsigned test build",
    )
    args = parser.parse_args(argv)
    result = build(args.target, source=args.source, signed=args.signed, version=args.version)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
