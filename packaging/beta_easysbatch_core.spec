import os
from pathlib import Path


binary_name = os.environ.get("BETA_EASYSBATCH_CORE_NAME", "BetaEasySbatchCore")
root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "packaging" / "beta_core_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=["yaml"],
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
