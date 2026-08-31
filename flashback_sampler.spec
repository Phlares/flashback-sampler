# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for flashback-sampler (Windows onedir build).

Build:
    pyinstaller flashback_sampler.spec --noconfirm

Outputs:
    dist/flashback-sampler/flashback-sampler.exe
    dist/flashback-sampler/ (dependencies + data files)

Notes:
    - No pip package ships native DLLs any more; the only native binary
      is flashback_core.dll (below).
    - The Monaspace OTF fonts are loaded at runtime via
      QFontDatabase.addApplicationFont(), so they must be copied to
      the bundle as data files.
    - flashback_core.dll (the Zig core, built via `zig build
      -Doptimize=ReleaseSafe` in core/) is bundled explicitly rather
      than discovered by collect_all, since it's our own build output,
      not a pip package. Destination is flashback_sampler/core -- the
      first path native.py's _candidates() checks, so the bundled app
      finds it exactly like a dev checkout finds its zig-out build.
"""

datas = []
binaries = []
hiddenimports = [
    "flashback_sampler.io",
]

# Bundle the Monaspace OTFs. Path relative to the spec file, which
# lives at the repo root.
datas.append(("flashback_sampler/app/fonts", "flashback_sampler/app/fonts"))


a = Analysis(
    ["flashback_sampler/app/main.py"],
    pathex=[],
    binaries=binaries + [
        ("core/zig-out/bin/flashback_core.dll", "flashback_sampler/core"),
    ],
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="flashback-sampler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep the console for diagnostic prints
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="flashback-sampler",
)
