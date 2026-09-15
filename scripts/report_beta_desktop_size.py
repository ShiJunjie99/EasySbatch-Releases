"""Report native Beta desktop artifact and embedded-runtime sizes after a cloud build."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def build_report(target: str, target_root: Path, artifacts_root: Path) -> dict[str, object]:
    components = []
    if target_root.is_dir():
        for path in sorted(target_root.iterdir()):
            components.append({"name": path.name, "bytes": tree_size(path)})
    artifact_files = []
    if artifacts_root.is_dir():
        for path in sorted(artifacts_root.iterdir()):
            if path.is_file():
                artifact_files.append({"name": path.name, "bytes": path.stat().st_size})
    largest = []
    if target_root.is_dir():
        files = (
            (path.stat().st_size, path.relative_to(target_root).as_posix())
            for path in target_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        largest = [{"name": name, "bytes": size} for size, name in sorted(files, reverse=True)[:20]]
    return {
        "schemaVersion": 1,
        "target": target,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "targetRootBytes": tree_size(target_root) if target_root.exists() else 0,
        "stagedArtifactBytes": tree_size(artifacts_root) if artifacts_root.exists() else 0,
        "components": components,
        "artifactFiles": artifact_files,
        "largestFiles": largest,
    }


def markdown(report: dict[str, object]) -> str:
    lines = [
        f"## Beta EasySbatch size report: {report['target']}",
        "",
        f"- Native target workspace: {format_bytes(int(report['targetRootBytes']))}",
        f"- Staged release files: {format_bytes(int(report['stagedArtifactBytes']))}",
        "",
        "### Staged release files",
        "",
        "| File | Size |",
        "| --- | ---: |",
    ]
    for item in report["artifactFiles"]:
        lines.append(f"| `{item['name']}` | {format_bytes(int(item['bytes']))} |")
    lines.extend(["", "### Build components", "", "| Component | Size |", "| --- | ---: |"])
    for item in sorted(report["components"], key=lambda value: int(value["bytes"]), reverse=True):
        lines.append(f"| `{item['name']}` | {format_bytes(int(item['bytes']))} |")
    lines.extend(["", "### Largest embedded/build files", "", "| File | Size |", "| --- | ---: |"])
    for item in report["largestFiles"]:
        lines.append(f"| `{item['name']}` | {format_bytes(int(item['bytes']))} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["win-x64", "mac-arm64"], required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.target, args.target_root, args.artifacts_root)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    rendered = markdown(report)
    args.markdown_out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
