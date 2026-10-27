# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the TgFarm desktop application.

A desktop tool that needs a Python interpreter and a `pip install` is not a
desktop tool. This produces `TgFarm.exe` with the whole runtime inside.

    pyinstaller packaging/tgfarm.spec --noconfirm

The output lands in `dist/TgFarm/`. Data files are declared explicitly because
the app reads its YAML config, prompts and UI from disk at runtime — PyInstaller
cannot see those references.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

# ---------------------------------------------------------------- data and modules
datas = [
    (str(ROOT / "config"), "config"),
    (str(ROOT / "tgfarm" / "ui"), "tgfarm/ui"),
    (str(ROOT / ".env.example"), "."),
]
datas += collect_data_files("tgfarm")

# These are imported lazily or by name at runtime, so the dependency walker does
# not see them: uvicorn's protocol/loop implementations, apscheduler's triggers,
# sqlmodel-free SQLAlchemy dialects, and the optional desktop stack.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "apscheduler.executors.asyncio",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.postgresql",
    "pydantic_settings",
    "feedparser",
    "dateutil.parser",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFilter",
    "PIL.ImageFont",
    # imported lazily inside functions, so the dependency walker never sees it
    "telethon",
    "telethon.tl",
    "telethon.tl.types",
    "webview",
    "pystray",
    "win32api",
]
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("apscheduler")

excludes = [
    "tkinter",  # pulls in Tcl/Tk for no benefit next to a WebView2 window
    "matplotlib",
    "pytest",
    "notebook",
    "IPython",
    "IPython.display",
]

analysis = Analysis(  # noqa: F821 - injected by PyInstaller
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure, analysis.zipped_data)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="TgFarm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # a desktop app, not a terminal
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "app.ico"),
)

collection = COLLECT(  # noqa: F821
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TgFarm",
)