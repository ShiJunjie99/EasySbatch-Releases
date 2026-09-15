import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


binary_name = os.environ.get("BETA_EASYSBATCH_CORE_NAME", "BetaEasySbatchCore")
root = Path(SPECPATH).parent

try:
    KEYRING_HIDDENIMPORTS = collect_submodules("keyring.backends")
except (ImportError, ModuleNotFoundError):
    KEYRING_HIDDENIMPORTS = []

a = Analysis(
    [str(root / "packaging" / "beta_core_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=["yaml", "paramiko"] + KEYRING_HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=binary_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
