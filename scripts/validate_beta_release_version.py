"""Validate a tagged Beta EasySbatch product version before release work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-beta\.(?:0|[1-9][0-9]*)"
)


def validate(version: str) -> None:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise RuntimeError("release version must use X.Y.Z-beta.N without leading zeros")
    notes = ROOT / "desktop" / "release-notes" / f"v{version}.md"
    if not notes.is_file():
        raise RuntimeError(f"release notes are missing: {notes}")
    build_source = (ROOT / "scripts" / "build_beta_desktop.py").read_text(encoding="utf-8")
    match = re.search(r'^DEFAULT_PRODUCT_VERSION = "([^"]+)"$', build_source, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise RuntimeError("tag version does not match DEFAULT_PRODUCT_VERSION")
    lock = json.loads((ROOT / "desktop" / "upstream.lock.json").read_text(encoding="utf-8"))
    if not isinstance(lock.get("commit"), str) or len(lock["commit"]) != 40:
        raise RuntimeError("the pinned DSH release identity is invalid")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    validate(args.version)
    print(args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
