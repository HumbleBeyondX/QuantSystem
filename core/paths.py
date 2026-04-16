"""Centralized path abstraction — works in three runtime contexts.

1. **Dev** — running from the source tree (python -m uvicorn ...):
     resource root = project root (this file's great-grandparent)
     user data dir = project root (keeps legacy behavior)

2. **PyInstaller-bundled backend** (qs-backend.exe):
     resource root = sys._MEIPASS (temporary extraction dir)
     user data dir = %APPDATA%/QuantSystem/ (set via QS_APP_DATA_DIR by Electron)

3. **Installed desktop app** — Electron spawns qs-backend.exe:
     Same as #2, with QS_APP_DATA_DIR pointing to Electron's app.getPath('userData')

All code that needs to read bundled defaults goes through ``get_resource_root``
or ``get_config_path``. All code that needs to WRITE (logs, DB, user-edited
configs) goes through ``get_user_data_dir`` / ``get_logs_dir`` / ``get_data_dir``.

``ensure_user_dir_scaffolded`` is called once at backend startup. It creates the
expected directory layout and copies default config templates into the user
dir on first run (never overwriting existing files).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "is_frozen",
    "get_resource_root",
    "get_user_data_dir",
    "get_config_path",
    "get_env_path",
    "get_logs_dir",
    "get_data_dir",
    "ensure_user_dir_scaffolded",
]


# ============================================================
# Context detection
# ============================================================

def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


# ============================================================
# Resource root (read-only, bundled)
# ============================================================

def get_resource_root() -> Path:
    """Where bundled read-only resources live.

    - Frozen: the PyInstaller temp extraction dir (``sys._MEIPASS``)
    - Dev:    the project root (``<this_file>/../..``)

    Access to ``config/``, default YAMLs, and other shipped assets goes here.
    This directory is NEVER written to — in the frozen case it's wiped on
    process exit.
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    # Dev mode: core/paths.py → core/ → project root
    return Path(__file__).resolve().parent.parent


# ============================================================
# User data dir (read-write, persistent)
# ============================================================

def get_user_data_dir() -> Path:
    """Where user-writable state lives.

    Priority:
      1. ``$QS_APP_DATA_DIR`` env var (Electron sets this to app.getPath('userData'))
      2. ``%APPDATA%/QuantSystem/`` on Windows
      3. ``~/Library/Application Support/QuantSystem/`` on macOS
      4. ``~/.quantsystem/`` on Linux
      5. Dev fallback: project root (same as get_resource_root() in dev mode)
    """
    override = os.environ.get("QS_APP_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    # Dev fallback — keep legacy behavior when running from source outside the
    # installed app. This lets ``python -m uvicorn dashboard.backend.main:app``
    # keep writing to ./data and ./logs without surprises.
    if not is_frozen() and not os.environ.get("QS_USE_APPDATA_IN_DEV"):
        return get_resource_root()

    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "QuantSystem"
        return Path.home() / "AppData" / "Roaming" / "QuantSystem"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "QuantSystem"

    # Linux / other
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "QuantSystem"
    return Path.home() / ".local" / "share" / "QuantSystem"


def get_logs_dir() -> Path:
    d = get_user_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_data_dir() -> Path:
    d = get_user_data_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config_dir() -> Path:
    d = get_user_data_dir() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# Config file resolution — user dir first, fall back to bundled defaults
# ============================================================

def get_config_path(name: str) -> Path:
    """Return the path to a config file.

    Checks ``<user_data>/config/<name>`` first. If it doesn't exist,
    returns ``<resource_root>/config/<name>`` (the bundled default).

    Callers should treat the returned path as READ-ONLY — write operations
    on user-editable configs should go to ``get_config_dir() / name``.
    """
    user_path = get_user_data_dir() / "config" / name
    if user_path.exists():
        return user_path
    return get_resource_root() / "config" / name


def get_env_path() -> Path:
    """Path to the .env file in the user data dir."""
    return get_user_data_dir() / ".env"


# ============================================================
# First-run scaffolding
# ============================================================

_ENV_TEMPLATE = """\
# QuantSystem — Environment Variables
# ------------------------------------------------------------
# Edit this file to configure API keys and recipient addresses.
# All values are optional for paper trading / monitoring only.
#
# OKX API credentials (for crypto live/paper trading via CCXT):
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=

# Daily report + risk alert recipient (defaults to humblebeyondx@gmail.com
# if unset; Gmail OAuth2 credentials are configured separately in
# config/gmail_credentials.json and config/gmail_token.json).
ALERT_EMAIL_RECIPIENT=

# Optional: fallback SMTP credentials (used only if OAuth2 is not set up).
# Generate a Gmail App Password at https://myaccount.google.com/apppasswords
ALERT_EMAIL_SENDER=
ALERT_EMAIL_PASSWORD=

# Optional: override where QuantSystem stores logs/data (set by Electron,
# do not set this manually unless you know what you're doing).
# QS_APP_DATA_DIR=
"""


def ensure_user_dir_scaffolded(
    default_configs: Optional[list[str]] = None,
) -> None:
    """Create the user data directory tree and seed default configs.

    Safe to call multiple times — only copies files that don't already exist
    and never overwrites user edits.

    Args:
        default_configs: list of config filenames (relative to ``config/``)
                         to copy from the resource root into the user dir.
                         Defaults to a built-in list if None.
    """
    user = get_user_data_dir()

    # Make the standard subdirectories
    for sub in ("config", "config/live", "config/strategies", "data", "logs"):
        (user / sub).mkdir(parents=True, exist_ok=True)

    # Copy default configs that have user-relevant settings (things the user
    # is expected to tweak — VPS lists, report times, strategy tuning).
    # Deliberately excludes anything that ships with API keys or secrets.
    if default_configs is None:
        default_configs = [
            "deployments.yaml",
            "reports.yaml",
            "live/eth_sniper_live.yaml",
            "live/btc_sniper_live.yaml",
            "strategies/eth_sniper.yaml",
        ]

    resource = get_resource_root()
    for rel in default_configs:
        src = resource / "config" / rel
        dst = user / "config" / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError:
                # Non-fatal — the app can fall back to the bundled default
                pass

    # Seed .env template if the user hasn't created one yet
    env_path = get_env_path()
    if not env_path.exists():
        try:
            env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
