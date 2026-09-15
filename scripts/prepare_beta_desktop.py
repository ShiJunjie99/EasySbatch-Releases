"""Materialize the pinned DeepSeek Harness source plus Beta EasySbatch overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import ssl
import stat
import subprocess
import tarfile
import tempfile
import urllib.request

import certifi


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
LOCK_PATH = DESKTOP / "upstream.lock.json"
OVERLAY = DESKTOP / "dsh-overlay"
PATCHES = DESKTOP / "dsh-patches"
DEFAULT_OUTPUT = ROOT / "build" / "beta-easysbatch" / "dsh"
MARKER = ".beta-easysbatch-prepared.json"


def _lock() -> dict[str, str]:
    value = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    required = {"repository", "tag", "commit", "archive_url", "archive_sha256", "license"}
    if not isinstance(value, dict) or set(value) != required or any(
        not isinstance(value[key], str) or not value[key] for key in required
    ):
        raise RuntimeError(f"invalid upstream lock: {LOCK_PATH}")
    if len(value["commit"]) != 40 or len(value["archive_sha256"]) != 64:
        raise RuntimeError(f"invalid pinned hashes in {LOCK_PATH}")
    return value


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _download_archive(lock: dict[str, str], cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"deepseek-harness-{lock['tag']}.tar.gz"
    if archive.exists() and _digest(archive) == lock["archive_sha256"]:
        return archive
    archive.unlink(missing_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        request = urllib.request.Request(
            lock["archive_url"],
            headers={"User-Agent": "Beta-EasySbatch-build/1"},
        )
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=60, context=context) as response, temporary.open("wb") as destination:
            shutil.copyfileobj(response, destination, length=1024 * 1024)
        actual = _digest(temporary)
        if actual != lock["archive_sha256"]:
            raise RuntimeError(f"upstream archive checksum mismatch: expected {lock['archive_sha256']}, got {actual}")
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def _extract_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        symlink_paths: set[tuple[str, ...]] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise RuntimeError(f"unsafe path in upstream archive: {member.name}")
            if member.islnk() or member.isdev():
                raise RuntimeError(f"unsupported hard link or device in upstream archive: {member.name}")
            if member.issym():
                target = PurePosixPath(member.linkname)
                resolved = posixpath.normpath(str(path.parent / target))
                archive_root = path.parts[0]
                if target.is_absolute() or not (resolved == archive_root or resolved.startswith(archive_root + "/")):
                    raise RuntimeError(f"unsafe symbolic link in upstream archive: {member.name}")
                symlink_paths.add(path.parts)
        for member in members:
            parts = PurePosixPath(member.name).parts
            if any(parts[:len(link)] == link and len(parts) > len(link) for link in symlink_paths):
                raise RuntimeError(f"archive member traverses a symbolic link: {member.name}")
        # GitHub source archives contain documentation/test symlinks. They are
        # irrelevant to the desktop build, and creating them is not reliably
        # permitted on Windows runners. Their paths/targets were validated
        # above; omit them rather than changing their meaning into copies.
        source.extractall(destination, members=[member for member in members if not member.issym()])
    roots = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("upstream archive must contain exactly one root directory")
    return roots[0]


def _copy_source(source: Path, destination: Path, expected_commit: str) -> None:
    source = source.resolve(strict=True)
    if not (source / "package.json").is_file():
        raise RuntimeError(f"source is not a DeepSeek Harness checkout: {source}")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if commit != expected_commit:
        raise RuntimeError(f"source commit mismatch: expected {expected_commit}, got {commit}")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=source,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if status:
        raise RuntimeError(f"source checkout must be clean: {source}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "node_modules", "lib", "dist", ".desktop-build"),
    )


def _apply_product_layer(root: Path, lock: dict[str, str]) -> None:
    shutil.copytree(OVERLAY, root, dirs_exist_ok=True)
    presets = root / "packages" / "preset" / "agent-presets" / "presets"
    for child in presets.iterdir():
        if child.name != "beta-easysbatch":
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    for patch in sorted(PATCHES.glob("*.patch")):
        environment = os.environ.copy()
        # Generated worktrees normally live below this repository. Prevent
        # `git apply` from discovering the parent checkout and resolving paths
        # against the wrong root.
        environment["GIT_CEILING_DIRECTORIES"] = str(root.parent)
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=root, env=environment, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=root, env=environment, check=True)
    marker = {
        "product": "Beta EasySbatch",
        "upstream_repository": lock["repository"],
        "upstream_tag": lock["tag"],
        "upstream_commit": lock["commit"],
        "upstream_archive_sha256": lock["archive_sha256"],
        "shipped_agent_presets": ["beta-easysbatch"],
        "submission_enabled": False,
        "user_confirmed_submission_supported": True,
    }
    (root / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def prepare(output: Path, *, source: Path | None = None) -> Path:
    lock = _lock()
    output = output.absolute()
    if output.is_symlink():
        raise RuntimeError(f"refusing a symbolic-link output directory: {output}")
    output = output.resolve()
    protected = {Path(output.anchor), Path.home().resolve(), ROOT.resolve(), ROOT.parent.resolve()}
    if output in protected:
        raise RuntimeError(f"refusing unsafe output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        marker = output / MARKER
        try:
            marker_info = os.lstat(marker)
            marker_value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            marker_info = None
            marker_value = None
        if (
            marker_info is None
            or not stat.S_ISREG(marker_info.st_mode)
            or stat.S_ISLNK(marker_info.st_mode)
            or not isinstance(marker_value, dict)
            or marker_value.get("product") != "Beta EasySbatch"
        ):
            raise RuntimeError(f"refusing to replace an unowned directory: {output}")
        shutil.rmtree(output)
    with tempfile.TemporaryDirectory(prefix="beta-easysbatch-prepare-", dir=output.parent) as temporary_name:
        temporary = Path(temporary_name)
        staged = temporary / "dsh"
        if source is not None:
            _copy_source(source, staged, lock["commit"])
        else:
            archive = _download_archive(lock, ROOT / "build" / "beta-easysbatch" / "cache")
            extracted = _extract_archive(archive, temporary / "archive")
            os.replace(extracted, staged)
        _apply_product_layer(staged, lock)
        os.replace(staged, output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source", type=Path, help="exact pinned local DSH checkout; network-free")
    args = parser.parse_args(argv)
    result = prepare(args.output, source=args.source)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
