"""Fail a release build if a Launcher artifact contains secret material markers."""

import argparse
from pathlib import Path


FORBIDDEN = (
    b"BEGIN OPENSSH PRIVATE KEY",
    b"BEGIN PRIVATE KEY",
    b"DEEPSEEK_API_KEY",
    b"SBATCH_AGENT_LOCAL_AI_KEY",
    b"SSH_PASSWORD",
    b"M10B3_BOOTSTRAP_SECRET_DO_NOT_LOG",
    b"M10B5B_TEST_API_KEY_DO_NOT_LOG",
    b"M10B5B_TEST_PROXY_SECRET_DO_NOT_LOG",
    b"M10B6_TEST_PASSWORD_DO_NOT_LOG",
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    files = []
    for value in args.paths:
        if value.is_symlink():
            parser.error(f"symlink is not a release artifact: {value.name}")
        if value.is_dir():
            files.extend(path for path in value.rglob("*") if path.is_file() and not path.is_symlink())
        elif value.is_file():
            files.append(value)
        else:
            parser.error(f"artifact does not exist: {value.name}")
    for path in files:
        content = path.read_bytes()
        for marker in FORBIDDEN:
            if marker in content:
                parser.error(f"forbidden secret marker in artifact: {path.name}")
    print(f"artifact secret scan passed: {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
