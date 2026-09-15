# PyInstaller specification shared by Linux, Windows and macOS native runners.

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

try:
    KEYRING_HIDDENIMPORTS = collect_submodules("keyring.backends")
except (ImportError, ModuleNotFoundError):
    # Local development environments may intentionally omit keyring.  Native
    # release builds install the declared dependency and collect all backends.
    KEYRING_HIDDENIMPORTS = []

try:
    import certifi
    CERTIFI_DATA = [(certifi.where(), "certifi")]
except (ImportError, OSError):
    CERTIFI_DATA = []


ROOT = Path(SPECPATH).parent
NAME = os.environ.get("EASYSBATCH_BINARY_NAME", "EasySbatch")

analysis = Analysis(
    [str(ROOT / "packaging" / "launcher_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "src" / "sbatch_agent" / "launcher_alpha.toml"), "sbatch_agent"),
        (str(ROOT / "src" / "sbatch_agent" / "launcher_build.json"), "sbatch_agent"),
    ] + CERTIFI_DATA,
    hiddenimports=KEYRING_HIDDENIMPORTS + ["certifi"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
