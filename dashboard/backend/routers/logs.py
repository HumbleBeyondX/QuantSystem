"""Log viewer API — list, tail, and export log files.

All endpoints operate on files inside ``get_logs_dir()`` (user-writable area).
Paths are sanitized against directory traversal.

Endpoints
---------
GET  /api/logs                     — list log files with size/mtime
GET  /api/logs/{filename}          — tail last N lines (?lines=200)
POST /api/logs/export              — zip all logs, return path + URL
GET  /api/logs/download/{filename} — raw file download
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.logging import get_logger
from core.paths import get_data_dir, get_logs_dir

logger = get_logger(__name__)
router = APIRouter(prefix="/api/logs", tags=["Logs"])


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────

class LogFileInfo(BaseModel):
    name: str
    size_bytes: int
    size_human: str
    modified_at: str
    lines_estimate: int


class TailResponse(BaseModel):
    filename: str
    lines: List[str]
    total_returned: int
    truncated: bool


class ExportResponse(BaseModel):
    path: str
    filename: str
    size_bytes: int
    download_url: str


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _safe_log_path(filename: str) -> Path:
    """Resolve a filename safely inside the logs dir. Rejects traversal."""
    logs_dir = get_logs_dir().resolve()
    candidate = (logs_dir / filename).resolve()
    try:
        candidate.relative_to(logs_dir)
    except ValueError:
        raise HTTPException(400, f"Invalid log filename: {filename}")
    return candidate


def _tail_lines(path: Path, n: int, max_bytes: int = 5_000_000) -> tuple[List[str], bool]:
    """Read the last N lines of a file efficiently."""
    if not path.exists():
        raise HTTPException(404, f"Log file not found: {path.name}")

    size = path.stat().st_size
    truncated = size > max_bytes

    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(-max_bytes, 2)
            # Skip partial line
            f.readline()
        else:
            f.seek(0)
        data = f.read()

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")

    lines = text.splitlines()
    return lines[-n:] if len(lines) > n else lines, truncated


# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────

@router.get("", response_model=List[LogFileInfo])
async def list_logs():
    """List all log files in the logs directory."""
    logs_dir = get_logs_dir()
    files: List[LogFileInfo] = []

    for path in sorted(logs_dir.rglob("*"), key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True):
        if not path.is_file():
            continue
        # Only show text-like files
        if path.suffix.lower() not in (".log", ".jsonl", ".txt", ".out", ""):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = path.relative_to(logs_dir).as_posix()
        # Rough line count estimate (size / avg line length)
        lines_est = int(stat.st_size / 120) if stat.st_size else 0
        files.append(LogFileInfo(
            name=rel,
            size_bytes=stat.st_size,
            size_human=_human_size(stat.st_size),
            modified_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            lines_estimate=lines_est,
        ))

    return files


@router.get("/tail")
async def tail_log(
    filename: str = Query(..., description="Log file name (relative to logs dir)"),
    lines: int = Query(200, ge=1, le=5000),
    level: Optional[str] = Query(None, description="Filter by level (INFO/WARN/ERROR)"),
):
    """Tail the last N lines of a log file. Optional level filter."""
    path = _safe_log_path(filename)
    tail, truncated = _tail_lines(path, lines)

    if level:
        lvl = level.lower()
        filtered: List[str] = []
        for line in tail:
            ll = line.lower()
            if f'"level": "{lvl}"' in ll or f"[{lvl}" in ll or f" {lvl} " in ll:
                filtered.append(line)
        tail = filtered

    return TailResponse(
        filename=filename,
        lines=tail,
        total_returned=len(tail),
        truncated=truncated,
    )


@router.post("/export", response_model=ExportResponse)
async def export_logs():
    """Zip the entire logs directory for a support bundle."""
    logs_dir = get_logs_dir()
    out_dir = get_data_dir() / "support"
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    zip_name = f"quantsystem-logs-{timestamp}.zip"
    zip_path = out_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in logs_dir.rglob("*"):
            if f.is_file():
                arcname = f.relative_to(logs_dir).as_posix()
                try:
                    zf.write(f, arcname)
                except (OSError, PermissionError):
                    # Skip files that can't be read (locked by another process)
                    continue

    size = zip_path.stat().st_size
    logger.info("logs_exported", path=str(zip_path), size_bytes=size)
    return ExportResponse(
        path=str(zip_path),
        filename=zip_name,
        size_bytes=size,
        download_url=f"/api/logs/download-support/{zip_name}",
    )


@router.get("/download/{filename:path}")
async def download_log(filename: str):
    """Download a raw log file."""
    path = _safe_log_path(filename)
    if not path.exists():
        raise HTTPException(404, f"File not found: {filename}")
    return FileResponse(path, filename=path.name, media_type="text/plain")


@router.get("/download-support/{filename}")
async def download_support_bundle(filename: str):
    """Download a previously-exported support bundle zip."""
    out_dir = get_data_dir() / "support"
    path = (out_dir / filename).resolve()
    try:
        path.relative_to(out_dir.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid path")
    if not path.exists():
        raise HTTPException(404, "Export not found — POST to /export first")
    return FileResponse(path, filename=filename, media_type="application/zip")
