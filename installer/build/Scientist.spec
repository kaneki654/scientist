# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

datas = []
binaries = []
hiddenimports = ['PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont', 'PIL.ImageFilter', 'PIL.ImageChops', 'PIL.ImageGrab']
datas += collect_data_files('uiautomator2')
binaries += collect_dynamic_libs('winrt')
hiddenimports += collect_submodules('uiautomator2')
hiddenimports += collect_submodules('winrt')


a = Analysis(
    ['C:/Users/ASUS TUF/Folder/scientist/Scientist.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'matplotlib'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Scientist',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:/Users/ASUS TUF/Folder/scientist/assets/scientist.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Scientist',
)
