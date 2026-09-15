import importlib.util
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
PREPARE_PATH = ROOT / "scripts" / "prepare_beta_desktop.py"
SPEC = importlib.util.spec_from_file_location("prepare_beta_desktop", PREPARE_PATH)
assert SPEC and SPEC.loader
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


def test_upstream_lock_is_exact_and_checksum_shaped():
    lock = json.loads((ROOT / "desktop/upstream.lock.json").read_text(encoding="utf-8"))
    assert lock["tag"] == "dsh-v0.1.5-rc.2"
    assert lock["commit"] == "fb2c4b9e698e30edb738bca4cf0618587db7d203"
    assert len(lock["archive_sha256"]) == 64
    int(lock["archive_sha256"], 16)


def test_product_overlay_ships_only_restricted_agent_preset():
    preset_root = ROOT / "desktop/dsh-overlay/packages/preset/agent-presets/presets"
    presets = sorted(path.name for path in preset_root.iterdir() if path.is_dir())
    assert presets == ["beta-easysbatch"]
    composition = (preset_root / "beta-easysbatch/agent.cordis.yml").read_text(encoding="utf-8")
    assert "@beta-easysbatch/dsh-tool" in composition
    for forbidden in ("dsh-tool-bash", "dsh-tool-pwsh", "dsh-tool-fs", "dsh-tool-web", "dsh-tool-subagent"):
        assert forbidden not in composition


def test_bundle_disables_telemetry_and_user_presets():
    patch = (ROOT / "desktop/dsh-overlay/packages/easysbatch/dsh-bundle/cordis.patch.yml").read_text(
        encoding="utf-8"
    )
    assert "session-telemetry-otel" in patch
    assert "includeUserRoot: false" in patch
    assert "default: beta-easysbatch" in patch


def test_archive_extraction_omits_safe_symlinks_for_windows(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        root = tarfile.TarInfo("source")
        root.type = tarfile.DIRTYPE
        target.addfile(root)
        body = b"safe\n"
        regular = tarfile.TarInfo("source/target.txt")
        regular.size = len(body)
        target.addfile(regular, io.BytesIO(body))
        link = tarfile.TarInfo("source/link.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "target.txt"
        target.addfile(link)

    extracted = PREPARE._extract_archive(archive, tmp_path / "out")
    assert (extracted / "target.txt").read_bytes() == b"safe\n"
    assert not (extracted / "link.txt").exists()


def test_pinned_patch_applies_to_exact_upstream_reference():
    reference = Path("/tmp/dsh-beta-easysbatch-reference")
    if not reference.is_dir():
        return
    # The developer reference may contain the patch already. Use Git's reverse
    # check in that case; either direction proves the stored patch matches.
    patch = ROOT / "desktop/dsh-patches/0001-beta-easysbatch-desktop.patch"
    import subprocess
    forward = subprocess.run(["git", "apply", "--check", str(patch)], cwd=reference)
    if forward.returncode != 0:
        reverse = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=reference)
        assert reverse.returncode == 0
