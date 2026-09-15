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
    assert "@deepseek-ai/dsh-easysbatch-product" in patch


def test_desktop_patch_adds_custom_packages_to_clean_host_build():
    patch = (ROOT / "desktop/dsh-patches/0001-beta-easysbatch-desktop.patch").read_text(encoding="utf-8")
    assert '"./packages/easysbatch/dsh-tool"' in patch
    assert '"./packages/easysbatch/dsh-product/tsconfig.host.json"' in patch
    assert '"./packages/easysbatch/dsh-product/tsconfig.client.json"' in patch
    assert "'packages/easysbatch/dsh-product'" in patch
    assert '"./packages/easysbatch/dsh-bundle"' in patch


def test_product_ui_owns_history_cluster_and_explicit_submission():
    product = ROOT / "desktop/dsh-overlay/packages/easysbatch/dsh-product"
    client = (product / "src/client/index.tsx").read_text(encoding="utf-8")
    host = (product / "src/index.ts").read_text(encoding="utf-8")
    tool = (ROOT / "desktop/dsh-overlay/packages/easysbatch/dsh-tool/src/index.ts").read_text(encoding="utf-8")
    assert "任务记录" in client
    assert "新建任务" in client
    assert "生成脚本预览" in client
    assert "保存到任务记录" in client
    assert "服务器项目目录" in client
    assert "使用集群默认值" in client
    assert "推荐分区与节点布局" in client
    assert "查找内存/时限依据" in client
    assert "只读扫描" in client
    assert "智能草稿" in client
    assert "集群资源" in client
    assert "window.confirm" in client
    assert "submitJob(job.id, job.id)" in client
    assert "@Remote('submitJob')" in host
    assert "@Remote('renderJob')" in host
    assert "@Remote('createJob')" in host
    assert "@Remote('listCatalog')" in host
    assert "@Remote('browseRemoteDirectory')" in host
    assert "@Remote('scanRemoteProject')" in host
    assert "@Remote('recommendResourceValues')" in host
    assert "@Remote('finalizePreparation')" in host
    assert "选择当前目录" in client
    assert "easysbatch_recommend_job" in tool
    assert "easysbatch_recommend_resource_values" in tool
    assert "easysbatch_list_profiles" in tool
    assert "easysbatch_list_catalog" in tool
    assert "easysbatch_prepare_job" in tool
    assert "easysbatch_revise_preparation" in tool
    assert "start_preparation" in tool
    assert "callCore('review_job'" in tool
    assert "consider_all_partitions" in tool
    assert "name: 'easysbatch_submit_job'" not in tool
    assert "登录并连接" in client
    assert 'type="password"' in client
    assert "inspectSshHostKey" in client
    assert "服务器 IP 为 10.158.132.77" in client
    assert "SSH 端口为 3088" in client
    assert "@Remote('inspectSshHostKey')" in host
    assert "callCore('connect_cluster'" in host
    assert "10.158.132.77" in client
    assert "profiles_path: paths().profilesPath" in host
    assert "catalog_path: paths().catalogPath" in host


def test_known_cluster_preset_excludes_personal_deployment_paths():
    source = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "src/sbatch_agent/desktop_profiles.py",
            "src/sbatch_agent/desktop_catalog.py",
        )
    )
    assert "/home/share/" in source
    assert "/home/shijunjie/" not in source
    assert "/mnt/sdc/" not in source
    assert "newtorch" not in source


def test_desktop_patch_uses_workspace_first_easysbatch_layout():
    patch = (ROOT / "desktop/dsh-patches/0001-beta-easysbatch-desktop.patch").read_text(encoding="utf-8")
    assert "SIDEBAR_DEFAULT = 220" in patch
    assert "RIGHTBAR_DEFAULT_RATIO = 0.50" in patch
    assert "actions.setExpanded(sessionId, true)" in patch
    assert "handle[data-side='rightbar']::after" in patch
    assert "'type.label': '项目文件'" in patch
    assert "'hero.headline': '告诉我你要运行什么'" in patch
    product = (
        ROOT
        / "desktop/dsh-overlay/packages/easysbatch/dsh-product/src/client/index.tsx"
    ).read_text(encoding="utf-8")
    assert "conversation.hero.brand.mark" in product


def test_desktop_patch_skips_seed_signing_only_for_unsigned_beta_builds():
    patch = (ROOT / "desktop/dsh-patches/0001-beta-easysbatch-desktop.patch").read_text(encoding="utf-8")
    assert "const unsigned = process.env.BETA_EASYSBATCH_UNSIGNED === '1'" in patch
    assert "if (targetPlatform === 'darwin' && !unsigned)" in patch


def test_signed_beta_uses_explicit_public_github_update_channel():
    patch = (ROOT / "desktop/dsh-patches/0001-beta-easysbatch-desktop.patch").read_text(
        encoding="utf-8"
    )
    update_patch = (ROOT / "desktop/dsh-patches/0002-beta-auto-update-channel.patch").read_text(
        encoding="utf-8"
    )
    build = (ROOT / "scripts/build_beta_desktop.py").read_text(encoding="utf-8")
    assert "provider: 'github'" in patch
    assert "owner: 'ShiJunjie99'" in patch
    assert "repo: 'EasySbatch-Releases'" in patch
    assert "channel: 'beta'" in patch
    assert "app-update.yml" in patch
    assert "allowPrerelease = true" in update_patch
    assert "this.updater.channel = 'beta'" in update_patch
    assert 'DEFAULT_PRODUCT_VERSION = "0.2.0-beta.1"' in build
    assert "GITHUB_REF_TYPE" in build and "GITHUB_REF_NAME" in build


def test_formal_beta_release_requires_native_signatures_and_no_unsigned_fallback():
    workflow = (ROOT / ".github/workflows/publish-beta-release.yml").read_text(encoding="utf-8")
    signing_patch = (ROOT / "desktop/dsh-patches/0003-beta-windows-pfx-signing.patch").read_text(
        encoding="utf-8"
    )
    build = (ROOT / "scripts/build_beta_desktop.py").read_text(encoding="utf-8")
    assert "environment: production-release" in workflow
    assert "WINDOWS_CERTIFICATE_PFX_BASE64" in workflow
    assert "MACOS_CERTIFICATE_P12_BASE64" in workflow
    assert "APPLE_API_KEY_P8_BASE64" in workflow
    assert "Get-AuthenticodeSignature" in workflow
    assert "xcrun stapler validate" in workflow
    assert "actions/attest-build-provenance@v3" in workflow
    assert "--signed" in workflow
    assert "-unsigned" not in workflow
    assert "VERIFIED_BUILD_RUN_ID" not in workflow
    assert "WIN_CSC_LINK" in signing_patch
    assert "WIN_CSC_KEY_PASSWORD" in signing_patch
    assert "PFX Windows signing" in build


def test_desktop_ci_matches_dsh_primary_node_runtime():
    workflow = (ROOT / ".github/workflows/build-beta-desktop.yml").read_text(encoding="utf-8")
    readme = (ROOT / "desktop/README.md").read_text(encoding="utf-8")
    assert 'NODE_VERSION: "24.18.0"' in workflow
    assert "Node 24.18.0" in readme
    assert "actions/setup-node@v6" in workflow
    assert "pnpm/action-setup@v6" in workflow


def test_desktop_packages_use_cloud_only_size_reporting_and_maximum_compression():
    workflow = (ROOT / ".github/workflows/build-beta-desktop.yml").read_text(encoding="utf-8")
    size_patch = (ROOT / "desktop/dsh-patches/0004-beta-package-size.patch").read_text(encoding="utf-8")
    build = (ROOT / "scripts/build_beta_desktop.py").read_text(encoding="utf-8")
    assert "report_beta_desktop_size.py" in workflow
    assert "GITHUB_STEP_SUMMARY" in workflow
    assert "\n          path: dist/beta-easysbatch/win-x64/*\n" not in workflow
    assert "\n          path: dist/beta-easysbatch/mac-arm64/*\n" not in workflow
    assert "compression: 'maximum'" in size_patch
    assert "_copy_release_artifacts" in build
    assert "shutil.copytree(upstream_artifacts" not in build


def test_archive_extraction_omits_safe_symlinks_for_windows(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        root = tarfile.TarInfo("source")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        target.addfile(root)
        body = b"safe\n"
        regular = tarfile.TarInfo("source/target.txt")
        regular.mode = 0o644
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
