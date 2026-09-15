"""Emit a compact, redacted GitHub Actions annotation from a build log."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
DIAGNOSTIC = re.compile(
    r"(?i)(?:^\s*(?:ERROR|FATAL)(?:\s|:)|\bError:|\bException:|Traceback|"
    r"Cannot find entry|Command failed|returned non-zero|exited with|Process completed|"
    r"killed|out of memory|ENOMEM|ERR_[A-Z_]+|ELIFECYCLE|TS\d{4}|\s⨯\s)"
)
SENSITIVE = re.compile(
    r"(?i)(?:authorization\s*:\s*(?:bearer|basic)|"
    r"(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=])"
)
# GitHub check-run annotation messages are truncated at 4096 characters. Keep
# enough headroom for multibyte text and workflow-command escaping.
MAX_ANNOTATION_BYTES = 3_500


def diagnostics(raw: str, *, context: int = 3) -> str:
    """Return diagnostic lines plus context without echoing secret-like lines."""
    lines = [ANSI.sub("", line) for line in raw.splitlines()]
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if DIAGNOSTIC.search(line):
            selected.update(range(max(0, index - context), min(len(lines), index + context + 1)))
    if not selected:
        selected.update(range(max(0, len(lines) - 40), len(lines)))
    result: list[str] = []
    previous: int | None = None
    for index in sorted(selected):
        if previous is not None and index != previous + 1:
            result.append("...")
        line = "[redacted sensitive diagnostic line]" if SENSITIVE.search(lines[index]) else lines[index]
        result.append(line)
        previous = index
    return "\n".join(result)


def _bounded(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_ANNOTATION_BYTES:
        return value
    marker = "\n... diagnostic annotation truncated ...\n"
    budget = (MAX_ANNOTATION_BYTES - len(marker.encode("utf-8"))) // 2
    start = encoded[:budget].decode("utf-8", errors="ignore")
    end = encoded[-budget:].decode("utf-8", errors="ignore")
    return start + marker + end


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    raw = args.log.read_text(encoding="utf-8", errors="replace")
    print(f"::error title={_escape(args.title)}::{_escape(_bounded(diagnostics(raw)))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
