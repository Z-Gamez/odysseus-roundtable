# -*- mode: python ; coding: utf-8 -*-
# Full standalone Odysseus: window host + FastAPI server + MCP servers in ONE
# onedir bundle (entry: standalone_app.py, which dispatches on argv).
# Build: venv\Scripts\python.exe -m PyInstaller --noconfirm OdysseusFull.spec
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
hiddenimports = ["app", "odysseus_app", "app_window"]

# Window host stack (same as the old Odysseus.spec).
for pkg in ("webview", "clr_loader", "pythonnet"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# Server stack: packages with dynamic imports / bundled data / native DLLs
# that PyInstaller's static analysis misses.
for pkg in ("uvicorn", "chromadb", "fastembed", "onnxruntime", "tokenizers",
            "tiktoken", "tiktoken_ext", "mcp", "playwright", "PIL",
            "bcrypt", "passlib"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
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
    excludes=["torch", "tensorflow"],  # never used; keep the bundle sane
    noarchive=False,
)
pyz = PYZ(a.pure)

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
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="static/odysseus.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OdysseusStandalone",
)
