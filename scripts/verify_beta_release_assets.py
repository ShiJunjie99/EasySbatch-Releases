"""Verify the exact signed Beta artifact set and updater metadata."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re


TARGETS = {
    "win-x64": {"os": "win", "update": "exe", "metadata": "beta.yml"},
    "mac-arm64": {"os": "mac", "update": "zip", "metadata": "beta-mac.yml"},
}


def _one(root: Path, name: str) -> Path:
    path = root / name
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"release artifact is missing or empty: {path}")
    return path


def _metadata(path: Path) -> tuple[str, str, int, str]:
    text = path.read_text(encoding="utf-8")
    fields = {}
    for field in ("version", "url", "size", "sha512"):
        match = re.search(rf"^\s*(?:-\s*)?{field}:\s*['\"]?([^'\"\r\n]+?)['\"]?\s*$", text, re.MULTILINE)
        if match is None:
            raise RuntimeError(f"{path.name} omits {field}")
        fields[field] = match.group(1).strip()
    try:
        size = int(fields["size"])
        base64.b64decode(fields["sha512"], validate=True)
    except (ValueError, TypeError):
        raise RuntimeError(f"{path.name} contains invalid update metadata") from None
    return fields["version"], fields["url"], size, fields["sha512"]


def verify_target(root: Path, target: str, version: str) -> None:
    details = TARGETS[target]
    base = f"Beta-EasySbatch-{version}-{details['os']}-{target.split('-', 1)[1]}"
    update = _one(root, f"{base}.{details['update']}")
    metadata = _one(root, details["metadata"])
    metadata_version, update_name, update_size, update_sha512 = _metadata(metadata)
    if metadata_version != version or update_name != update.name:
        raise RuntimeError(f"{metadata.name} does not reference {update.name} at {version}")
    actual_sha512 = base64.b64encode(hashlib.sha512(update.read_bytes()).digest()).decode("ascii")
    if update_size != update.stat().st_size or update_sha512 != actual_sha512:
        raise RuntimeError(f"{metadata.name} does not authenticate {update.name}")

    record = json.loads(_one(root, f"{target}-release.json").read_text(encoding="utf-8"))
    if record.get("schemaVersion") != 1 or record.get("target") != target or record.get("version") != version:
        raise RuntimeError(f"{target} package completion record does not match {version}")
    if target == "mac-arm64":
        _one(root, f"{base}.dmg")
        _one(root, f"{base}.zip.blockmap")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=[*TARGETS, "combined"], required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    targets = TARGETS if args.target == "combined" else (args.target,)
    for target in targets:
        verify_target(args.root, target, args.version)
    print(f"verified {args.target} {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
