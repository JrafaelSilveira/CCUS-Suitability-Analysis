# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CCUS Suitability Analysis desktop app.

Build with:
    pyinstaller --noconfirm CCUSApp.spec

The resulting distributable lives in dist/CCUSApp/. Ship that whole folder.
Users launch dist/CCUSApp/CCUSApp.exe; no Python or extra installer needed.
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

# Heavy third-party packages whose data files / dynamic imports PyInstaller
# can't infer just from `import`. collect_all returns (datas, binaries, hiddenimports).
datas = []
binaries = []
hiddenimports = []

for pkg in (
    "PySide6",
    "shiboken6",
    "vtkmodules",
    "pyvista",
    "pyvistaqt",
    "folium",
    "branca",
    "jinja2",
    "geopandas",
    "pyogrio",
    "shapely",
    "fiona",
    "rasterio",
    "pyproj",
    "matplotlib",
    "seaborn",
    "xyzservices",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:
        print(f"[spec] skipped {pkg}: {exc}")

# NOTE: assets/, data/, and output/figures/ are intentionally NOT bundled into
# the PyInstaller archive. The build_exe.ps1 script copies them next to the
# .exe instead, so they're visible inside dist/CCUSApp/ (easy to swap, easy to
# zip the whole folder, no hunting through _internal/). main.py chdir's into
# the .exe folder at startup so the existing relative paths still resolve.
#
# Views/ and models/ are Python packages and are picked up automatically by the
# Analysis phase — no need to list them here.

# Modules imported only dynamically (string-based imports, runtime importlib).
hiddenimports += [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel",
    "vtkmodules.all",
    "vtkmodules.util.numpy_support",
    "vtkmodules.util.data_model",
    "pyogrio._io",
    "pyogrio._geometry",
    "fiona.enums",
    "rasterio.sample",
    "rasterio.vrt",
    "rasterio._features",
    "rasterio._shim",
    "rasterio.control",
]


block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CCUSApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # --windowed: no terminal behind the app
    disable_windowed_traceback=False,
    icon="assets/logo.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CCUSApp",
)
