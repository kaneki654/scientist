# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

HERE = SPECPATH                       # the fbdaily/ folder
PARENT = os.path.dirname(HERE)        # the adb/ folder

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs
datas = collect_data_files('uiautomator2')

# bundle the essential adb files so the .exe is self-contained / portable
_pt = os.path.join(PARENT, 'platform-tools')
for _f in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll'):
    _p = os.path.join(_pt, _f)
    if os.path.exists(_p):
        datas.append((_p, 'platform-tools'))

# bundle the app icon(s) if the user dropped them in fbdaily/
for _ic in ('icon.png', 'icon.ico'):
    _p = os.path.join(HERE, _ic)
    if os.path.exists(_p):
        datas.append((_p, '.'))

# .exe file icon: fbdaily/icon.ico -> adb/icon.ico -> none
_exe_icon = None
for _cand in (os.path.join(HERE, 'icon.ico'), os.path.join(PARENT, 'icon.ico')):
    if os.path.exists(_cand):
        _exe_icon = _cand
        break

a = Analysis(
    ['gui.py'],
    pathex=[HERE, PARENT],
    binaries=collect_dynamic_libs('winrt'),
    datas=datas,
    hiddenimports=['fbdaily', 'fb', 'uiautomator2'] + collect_submodules('winrt'),
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
    name='FB Daily',
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
    icon=_exe_icon,
)
