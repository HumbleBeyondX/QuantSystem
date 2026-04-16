"""PyInstaller entry point for the QuantSystem backend.

Replaces ``python -m uvicorn dashboard.backend.main:app`` in the packaged
desktop app. Electron spawns the resulting ``qs-backend.exe`` during
application startup.

Responsibilities:
  * Ensure the user data dir scaffolding exists before any config reads
  * Honor QS_BACKEND_PORT / QS_BACKEND_HOST env vars (set by Electron)
  * Use the rotating file logger (configured in core/logging.py)
  * Run uvicorn directly (no shell, no subprocess overhead)

Do NOT import heavy modules at top-level here — that pulls them into the
PyInstaller analysis phase and bloats the bundle. The lifespan hook in
dashboard/backend/main.py handles everything else.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # Ensure the user data dir exists before anything else touches config.
    # Do this eagerly (not inside app.lifespan) because even importing a
    # router can read a config file.
    from core.paths import ensure_user_dir_scaffolded

    ensure_user_dir_scaffolded()

    host = os.environ.get("QS_BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("QS_BACKEND_PORT", "8002"))
    log_level = os.environ.get("QS_LOG_LEVEL", "info").lower()

    # Import uvicorn + app late so PyInstaller sees them through the main
    # entry import chain (which the spec file analyzes).
    import uvicorn
    from dashboard.backend.main import app

    print(
        f"[qs-backend] Starting on http://{host}:{port}  (log_level={log_level})",
        flush=True,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
