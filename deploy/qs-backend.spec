# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for qs-backend.exe (bundled FastAPI + live runner).

Build:
    pip install -r deploy/requirements-build.txt
    pyinstaller deploy/qs-backend.spec --clean --noconfirm

Output:
    deploy/dist/qs-backend/qs-backend.exe  (+ _internal/ sibling)

The bundle is one-dir (not one-file) because:
  * Cold-start is much faster (no temp extraction on every launch)
  * The whole folder is shipped as an Electron extraResource as-is
  * Easier to inspect / debug which deps shipped
"""

import sys
from pathlib import Path

# Resolve the project root: this spec lives in <root>/deploy/
SPEC_PATH = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_PATH.parent

block_cipher = None


# ──────────────────────────────────────────────────────────────
# Bundled data files (accessible via sys._MEIPASS at runtime)
# ──────────────────────────────────────────────────────────────
datas = [
    # Ship the config/ directory as a read-only default.
    # At runtime, core.paths.get_config_path() checks the user data dir
    # first, then falls back to this.
    (str(PROJECT_ROOT / "config"), "config"),
]

# Optional: include strategy templates if they're used for scaffolding.
templates_dir = PROJECT_ROOT / "strategies" / "templates"
if templates_dir.exists():
    datas.append((str(templates_dir), "strategies/templates"))


# ──────────────────────────────────────────────────────────────
# Hidden imports — PyInstaller's static analyzer misses these
# ──────────────────────────────────────────────────────────────
hiddenimports = [
    # Uvicorn machinery
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",

    # FastAPI + Pydantic
    "pydantic.deprecated",
    "pydantic.deprecated.decorator",
    "pydantic.deprecated.parse",
    "pydantic_core",

    # CCXT — registers exchanges lazily, PyInstaller needs to pre-import them
    "ccxt",
    "ccxt.async_support",
    "ccxt.async_support.okx",
    "ccxt.async_support.binance",
    "ccxt.async_support.bybit",
    "ccxt.pro",
    "ccxt.pro.okx",
    "ccxt.pro.binance",
    "ccxt.pro.bybit",

    # Strategies — dynamic class loading via strategies.registry
    "strategies.registry",
    "strategies.base",
    "strategies.crypto.sniper_strategy",
    "strategies.crypto.eth_sniper_strategy",
    "strategies.crypto.momentum_strategy",
    "strategies.crypto.swing_strategy",
    "strategies.crypto.scalping_strategy",
    "strategies.crypto.breakout_strategy",
    "strategies.crypto.event_strategy",
    "strategies.equity.factor_strategy",
    "strategies.equity.timing_strategy",
    "strategies.equity.long_short_strategy",
    "strategies.options.delta_neutral_strategy",
    "strategies.options.gamma_scalp_strategy",
    "strategies.options.spy_iron_condor_strategy",
    "strategies.options.spy_wheel_strategy",

    # Email / reports
    "yfinance",
    "google.oauth2.credentials",
    "google.auth.transport.requests",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery",
    "googleapiclient.discovery_cache",
    "googleapiclient.discovery_cache.file_cache",

    # Other
    "pandas_ta",
    "aiosqlite",
    "sqlalchemy.dialects.sqlite",
    "email.mime.multipart",
    "email.mime.text",
    "encodings.idna",
]


# ──────────────────────────────────────────────────────────────
# Exclusions — keep the bundle lean
# ──────────────────────────────────────────────────────────────
excludes = [
    # Heavy ML/RL libs — not used by the live runner
    "torch",
    "tensorflow",
    "keras",
    "stable_baselines3",
    "gymnasium",
    "mlflow",

    # Visualization — not used on the server side
    "matplotlib",
    "seaborn",
    "plotly",
    "IPython",
    "jupyter",
    "jupyter_client",
    "jupyterlab",
    "notebook",

    # Testing frameworks
    "pytest",
    "hypothesis",
    "coverage",

    # Dev tools
    "black",
    "mypy",
    "ruff",
]


# ──────────────────────────────────────────────────────────────
# Analysis
# ──────────────────────────────────────────────────────────────
a = Analysis(
    [str(PROJECT_ROOT / "deploy" / "backend_entry.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="qs-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX can trigger false-positive AV alerts — skip
    console=False,  # No cmd window flash when Electron spawns it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "desktop" / "icons" / "icon.ico") if (PROJECT_ROOT / "desktop" / "icons" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="qs-backend",
)
