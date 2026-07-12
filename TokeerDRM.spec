# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = [('web', 'web'), ('app_icon.ico', '.')]
binaries = [('extract_tickets.exe', '.')]
hiddenimports = ['appdirs']
datas += copy_metadata('appdirs')
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['tokeer_drm.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'matplotlib', 'numpy', 'tkinter'],
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
    name='TokeerDRM',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX disabled: UPX-packed DLLs (esp. the bundled WebView2 loader) extracted from
    # a onefile build to %TEMP% are a common trigger for AV false-positives / blocking
    # on SOME machines, which shows up as a blank webview window. Slightly larger exe,
    # far fewer "stuck on blank screen" reports.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app_icon.ico',
)
