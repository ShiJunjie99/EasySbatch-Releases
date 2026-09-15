"""Assemble a real macOS application bundle plus ZIP and DMG assets."""

import argparse
from pathlib import Path
import platform
import plistlib
import shutil
import stat
import subprocess


ARCHES = {"arm64", "x86_64"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=sorted(ARCHES), required=True)
    args = parser.parse_args(argv)
    if platform.system() != "Darwin" or platform.machine() != args.architecture:
        parser.error("macOS build architecture does not match native runner")
    root = Path(__file__).resolve().parents[1]
    app = args.output / f"EasySbatch-macOS-{args.architecture}.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(root / "packaging/macos/Info.plist", app / "Contents/Info.plist")
    plistlib.load((app / "Contents/Info.plist").open("rb"))
    shutil.copy2(root / "packaging/macos/EasySbatch", macos / "EasySbatch")
    shutil.copy2(root / "packaging/macos/launch.applescript", resources / "launch.applescript")
    shutil.copy2(args.binary, resources / "easysbatch-launcher")
    for executable in (macos / "EasySbatch", resources / "easysbatch-launcher"):
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    archive = args.output / f"EasySbatch-macOS-{args.architecture}.zip"
    archive.unlink(missing_ok=True)
    subprocess.run([
        "/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
        str(app), str(archive),
    ], check=True)
    image = args.output / f"EasySbatch-macOS-{args.architecture}.dmg"
    image.unlink(missing_ok=True)
    subprocess.run([
        "/usr/bin/hdiutil", "create", "-quiet", "-fs", "HFS+",
        "-volname", "EasySbatch Launcher", "-srcfolder", str(app), str(image),
    ], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
