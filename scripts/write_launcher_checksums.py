"""Create stable SHA256SUMS for approved Launcher release asset names."""

import argparse
import hashlib
from pathlib import Path
import re


ASSET = re.compile(
    r"(?:EasySbatch-Windows-x86_64\.exe|"
    r"EasySbatch-macOS-(?:arm64|x86_64)\.(?:zip|dmg)|"
    r"EasySbatch-Linux-x86_64)"
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    assets = sorted(
        path for path in args.directory.iterdir()
        if ASSET.fullmatch(path.name) and path.is_file() and not path.is_symlink()
    )
    if not assets:
        parser.error("no approved release assets found")
    lines = []
    for path in assets:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    (args.directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
