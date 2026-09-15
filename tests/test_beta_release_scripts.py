import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = load_script("verify_beta_release_assets")
VALIDATE = load_script("validate_beta_release_version")
BUILD = load_script("build_beta_desktop")


def write_target(root: Path, target: str, version: str) -> None:
    details = VERIFY.TARGETS[target]
    arch = target.split("-", 1)[1]
    base = f"Beta-EasySbatch-{version}-{details['os']}-{arch}"
    update = root / f"{base}.{details['update']}"
    update.write_bytes(f"signed-{target}".encode())
    digest = base64.b64encode(hashlib.sha512(update.read_bytes()).digest()).decode("ascii")
    (root / details["metadata"]).write_text(
        f"version: {version}\nfiles:\n  - url: {update.name}\n"
        f"    sha512: {digest}\n    size: {update.stat().st_size}\n",
        encoding="utf-8",
    )
    (root / f"{target}-release.json").write_text(
        json.dumps({"schemaVersion": 1, "target": target, "version": version}),
        encoding="utf-8",
    )
    if target == "mac-arm64":
        (root / f"{base}.dmg").write_bytes(b"signed-dmg")
        (root / f"{base}.zip.blockmap").write_bytes(b"blockmap")


def test_current_beta_version_has_matching_release_identity():
    VALIDATE.validate("0.2.0-beta.1")


def test_product_version_stamps_the_complete_dsh_release_family(tmp_path):
    manifests = [
        tmp_path / "package.json",
        tmp_path / "apps/desktop/package.json",
        tmp_path / "apps/cli/package.json",
        tmp_path / "packages/client/ui/package.json",
        tmp_path / "packages/easysbatch/dsh-tool/package.json",
        tmp_path / "packages/experimental/public-tool/package.json",
    ]
    for index, path in enumerate(manifests):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": f"package-{index}", "version": "0.1.5-rc.2"}), encoding="utf-8")
    BUILD._stamp_product_version(tmp_path, "0.2.0-beta.1")
    assert {
        json.loads(path.read_text(encoding="utf-8"))["version"] for path in manifests
    } == {"0.2.0-beta.1"}


def test_release_assets_authenticate_both_platform_updaters(tmp_path):
    write_target(tmp_path, "win-x64", "0.2.0-beta.1")
    write_target(tmp_path, "mac-arm64", "0.2.0-beta.1")
    VERIFY.verify_target(tmp_path, "win-x64", "0.2.0-beta.1")
    VERIFY.verify_target(tmp_path, "mac-arm64", "0.2.0-beta.1")


def test_release_assets_reject_changed_updater(tmp_path):
    write_target(tmp_path, "win-x64", "0.2.0-beta.1")
    (tmp_path / "Beta-EasySbatch-0.2.0-beta.1-win-x64.exe").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="does not authenticate"):
        VERIFY.verify_target(tmp_path, "win-x64", "0.2.0-beta.1")
