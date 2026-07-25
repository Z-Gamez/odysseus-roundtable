# -*- mode: python ; coding: utf-8 -*-
# macOS standalone Odysseus: window host + FastAPI server + MCP servers in ONE
# onedir .app (entry: standalone_app.py, argv dispatch — same architecture as
# the Windows OdysseusFull.spec, minus the Win32-only stack).
# Built by .github/workflows/build-macos.yml on a macOS runner:
#   python -m PyInstaller --noconfirm OdysseusMac.spec   -> dist/Odysseus.app
# The workflow generates installer/odysseus.icns from static/odysseus.ico
# before invoking PyInstaller.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("static", "static"),
    ("scripts", "scripts"),
    ("mcp_servers", "mcp_servers"),
    ("services/hwfit/data", "services/hwfit/data"),
    ("config", "config"),
    (".env.example", "."),
]
binaries = []
hiddenimports = ["app"]

# Server + window stack: packages with dynamic imports / bundled data / native
# libs that PyInstaller's static analysis misses. All guarded — a package that
# isn't installed on the runner is simply skipped (its feature degrades at
# runtime the same way it does from source).
for pkg in ("webview", "uvicorn", "chromadb", "fastembed", "onnxruntime",
            "tokenizers", "tiktoken", "tiktoken_ext", "mcp", "playwright",
            "PIL", "bcrypt", "passlib"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

# pywebview's Cocoa backend goes through pyobjc; make the frameworks explicit
# so lazy platform imports can't be missed.
for pkg in ("objc", "Foundation", "AppKit", "WebKit", "Security"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# Our own package trees (routes/src/core/services are imported dynamically in
# places; the bundled mcp_servers/*.py run via runpy against frozen modules).
for pkg in ("routes", "src", "core", "services", "mcp_servers"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

a = Analysis(
    ["standalone_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow", "clr_loader", "pythonnet"],
    noarchive=False,
)
pyz = PYZ(a.pure)

_icon = "installer/odysseus.icns" if os.path.exists("installer/odysseus.icns") else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Odysseus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=_icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OdysseusMac",
)
app = BUNDLE(
    coll,
    name="Odysseus.app",
    icon=_icon,
    bundle_identifier="com.zgamez.odysseus",
    info_plist={
        "CFBundleName": "Odysseus",
        "CFBundleDisplayName": "Odysseus",
        "CFBundleShortVersionString": "1.1.0",
        "CFBundleVersion": "1.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
    },
)
