"""Stamp public source revision metadata into a Launcher build tree."""

import argparse
import json
from pathlib import Path
import re


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("commit")
    parser.add_argument(
        "--output", type=Path,
        default=Path("src/sbatch_agent/launcher_build.json"),
    )
    args = parser.parse_args(argv)
    if re.fullmatch(r"[0-9a-f]{7,40}", args.commit) is None:
        parser.error("commit must be a lowercase Git object id")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"commit": args.commit}, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
