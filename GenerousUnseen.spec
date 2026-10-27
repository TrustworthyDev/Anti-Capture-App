# -*- mode: python ; coding: utf-8 -*-

# Paths are resolved relative to this .spec's own location (SPECPATH, injected
# by PyInstaller) so the build works from any checkout directory.
import os
BASE = SPECPATH

a = Analysis(
    [os.path.join(BASE, 'capture_gui.py')],
    pathex=[],
    binaries=[],
    datas=[(os.path.join(BASE, 'icon.ico'), '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GenerousUnseen',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=[os.path.join(BASE, 'icon.ico')],
)
