# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[("ui/styles/wp_style.qss", "ui/styles")],
    hiddenimports=["sqlalchemy.dialects.sqlite"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AutoPoster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
