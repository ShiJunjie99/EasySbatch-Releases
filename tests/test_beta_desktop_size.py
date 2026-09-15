import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/report_beta_desktop_size.py"
SPEC = importlib.util.spec_from_file_location("report_beta_desktop_size", SCRIPT)
assert SPEC and SPEC.loader
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def test_size_report_separates_staged_files_from_build_components(tmp_path):
    target_root = tmp_path / "target"
    artifacts_root = tmp_path / "staged"
    (target_root / "runtime").mkdir(parents=True)
    (target_root / "seed").mkdir()
    artifacts_root.mkdir()
    (target_root / "runtime" / "node").write_bytes(b"n" * 20)
    (target_root / "seed" / "package.tgz").write_bytes(b"s" * 10)
    (artifacts_root / "Beta-EasySbatch-0.2.0-beta.1-win-x64.exe").write_bytes(b"e" * 7)

    report = REPORT.build_report("win-x64", target_root, artifacts_root)

    assert report["targetRootBytes"] == 30
    assert report["stagedArtifactBytes"] == 7
    assert {item["name"]: item["bytes"] for item in report["components"]} == {
        "runtime": 20,
        "seed": 10,
    }
    rendered = REPORT.markdown(report)
    assert "Beta EasySbatch size report: win-x64" in rendered
    assert "Beta-EasySbatch-0.2.0-beta.1-win-x64.exe" in rendered
