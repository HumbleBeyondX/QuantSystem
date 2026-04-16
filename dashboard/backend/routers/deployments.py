"""Cloud Deployments API — proxy status/control to remote VPS instances.

The local dashboard acts as a unified control plane. Each remote VPS
runs a FastAPI backend with /api/live/runner/* endpoints. This router
proxies requests and aggregates status from all deployments.

Endpoints:
    GET  /api/deployments                     — List all deployments with live status
    GET  /api/deployments/{id}/status          — Full status of one deployment
    GET  /api/deployments/{id}/health          — Quick health check
    POST /api/deployments/{id}/emergency-stop  — Remote emergency stop
    POST /api/deployments/{id}/force-close     — Remote force close positions
    POST /api/deployments/{id}/restart         — Restart container via SSH
    GET  /api/deployments/{id}/logs            — Fetch recent container logs
    GET  /api/deployments/summary              — Aggregated P&L across all
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/deployments", tags=["deployments"])

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

_deployments: List[Dict[str, Any]] = []
_status_cache: Dict[str, Dict[str, Any]] = {}
_cache_ttl = 5  # seconds
_cache_timestamps: Dict[str, float] = {}

# Connection timeout for remote requests
_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=5)


def load_deployments(config_path: str = "config/deployments.yaml") -> None:
    """Load deployment configs from YAML."""
    global _deployments
    path = Path(config_path)
    if not path.exists():
        logger.warning("deployments_config_not_found", path=str(path))
        return

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    _deployments = data.get("deployments", [])
    logger.info("deployments_loaded", count=len(_deployments))


def get_deployment(deploy_id: str) -> Dict[str, Any]:
    """Get a deployment config by ID."""
    for d in _deployments:
        if d["id"] == deploy_id:
            return d
    raise HTTPException(404, f"Deployment not found: {deploy_id}")


def _base_url(dep: Dict[str, Any]) -> str:
    return f"http://{dep['host']}:{dep.get('port', 8002)}"


# ------------------------------------------------------------------
# Response Models
# ------------------------------------------------------------------

class DeploymentInfo(BaseModel):
    id: str
    name: str
    strategy_id: str
    host: str
    port: int
    region: str
    region_label: str
    exchange: str
    symbol: str
    asset_class: str
    enabled: bool
    tags: List[str] = []


class DeploymentStatus(BaseModel):
    deployment: DeploymentInfo
    online: bool
    runner_status: Optional[Dict[str, Any]] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    last_checked: Optional[str] = None


class ActionResponse(BaseModel):
    status: str
    message: str
    data: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _fetch_remote_status(dep: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch runner status from a remote VPS instance."""
    dep_id = dep["id"]

    # Check cache
    now = time.time()
    if dep_id in _cache_timestamps and (now - _cache_timestamps[dep_id]) < _cache_ttl:
        return _status_cache.get(dep_id, {})

    url = f"{_base_url(dep)}/api/live/runner/status"
    start = time.time()

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url) as resp:
                latency = (time.time() - start) * 1000
                if resp.status == 200:
                    data = await resp.json()
                    result = {
                        "online": True,
                        "runner_status": data,
                        "latency_ms": round(latency, 1),
                        "error": None,
                    }
                else:
                    result = {
                        "online": True,
                        "runner_status": None,
                        "latency_ms": round(latency, 1),
                        "error": f"HTTP {resp.status}",
                    }
    except asyncio.TimeoutError:
        result = {"online": False, "runner_status": None, "latency_ms": None, "error": "Timeout"}
    except Exception as e:
        result = {"online": False, "runner_status": None, "latency_ms": None, "error": str(e)}

    # Update cache
    _status_cache[dep_id] = result
    _cache_timestamps[dep_id] = now
    return result


def _find_ssh_key(dep: Dict[str, Any]) -> Optional[str]:
    """Resolve SSH key path."""
    key_path = dep.get("ssh_key_path", "")
    if not key_path:
        return None

    # Check common locations
    candidates = [
        Path(key_path),
        Path.home() / ".ssh" / key_path,
        Path("config") / "keys" / key_path,
        Path(key_path).expanduser(),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("", response_model=List[DeploymentStatus])
async def list_deployments():
    """List all deployments with live status."""
    if not _deployments:
        return []

    # Fetch status from all enabled deployments concurrently
    enabled = [d for d in _deployments if d.get("enabled", True)]

    async def fetch_one(dep):
        info = DeploymentInfo(
            id=dep["id"],
            name=dep.get("name", dep["id"]),
            strategy_id=dep.get("strategy_id", ""),
            host=dep["host"],
            port=dep.get("port", 8002),
            region=dep.get("region", ""),
            region_label=dep.get("region_label", ""),
            exchange=dep.get("exchange", ""),
            symbol=dep.get("symbol", ""),
            asset_class=dep.get("asset_class", ""),
            enabled=dep.get("enabled", True),
            tags=dep.get("tags", []),
        )
        status = await _fetch_remote_status(dep)
        return DeploymentStatus(
            deployment=info,
            online=status.get("online", False),
            runner_status=status.get("runner_status"),
            latency_ms=status.get("latency_ms"),
            error=status.get("error"),
            last_checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    results = await asyncio.gather(*[fetch_one(d) for d in enabled], return_exceptions=True)

    # Filter out exceptions
    return [r for r in results if isinstance(r, DeploymentStatus)]


@router.get("/summary")
async def deployments_summary():
    """Aggregated summary across all deployments."""
    statuses = await list_deployments()

    total_pnl = 0.0
    total_trades = 0
    total_orders = 0
    online_count = 0
    running_count = 0

    for s in statuses:
        if s.online:
            online_count += 1
        rs = s.runner_status
        if rs and rs.get("running"):
            running_count += 1
            total_pnl += rs.get("session_pnl", 0)
            total_trades += rs.get("session_trades", 0)
            total_orders += rs.get("session_orders", 0)

    return {
        "total_deployments": len(statuses),
        "online": online_count,
        "running": running_count,
        "total_session_pnl": round(total_pnl, 4),
        "total_session_trades": total_trades,
        "total_session_orders": total_orders,
    }


@router.get("/{deploy_id}/status")
async def deployment_status(deploy_id: str):
    """Get full status of a single deployment."""
    dep = get_deployment(deploy_id)
    status = await _fetch_remote_status(dep)
    return {
        "id": dep["id"],
        "name": dep.get("name"),
        **status,
    }


@router.get("/{deploy_id}/health")
async def deployment_health(deploy_id: str):
    """Quick health check for a deployment."""
    dep = get_deployment(deploy_id)
    url = f"{_base_url(dep)}/health"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            start = time.time()
            async with session.get(url) as resp:
                latency = (time.time() - start) * 1000
                data = await resp.json()
                return {
                    "id": dep["id"],
                    "healthy": data.get("status") == "healthy",
                    "latency_ms": round(latency, 1),
                }
    except Exception as e:
        return {
            "id": dep["id"],
            "healthy": False,
            "error": str(e),
        }


@router.post("/{deploy_id}/emergency-stop", response_model=ActionResponse)
async def deployment_emergency_stop(deploy_id: str):
    """Remote emergency stop."""
    dep = get_deployment(deploy_id)
    url = f"{_base_url(dep)}/api/live/runner/emergency-stop"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url) as resp:
                data = await resp.json()
                return ActionResponse(
                    status="ok",
                    message=f"Emergency stop sent to {dep['name']}",
                    data=data,
                )
    except Exception as e:
        raise HTTPException(502, f"Failed to reach {dep['name']}: {e}")


@router.post("/{deploy_id}/force-close", response_model=ActionResponse)
async def deployment_force_close(deploy_id: str):
    """Remote force close positions."""
    dep = get_deployment(deploy_id)
    url = f"{_base_url(dep)}/api/live/runner/force-close"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url) as resp:
                data = await resp.json()
                return ActionResponse(
                    status="ok",
                    message=f"Force close sent to {dep['name']}",
                    data=data,
                )
    except Exception as e:
        raise HTTPException(502, f"Failed to reach {dep['name']}: {e}")


@router.post("/{deploy_id}/restart", response_model=ActionResponse)
async def deployment_restart(deploy_id: str):
    """Restart the Docker container on the remote VPS via SSH."""
    dep = get_deployment(deploy_id)
    ssh_key = _find_ssh_key(dep)
    if not ssh_key:
        raise HTTPException(
            400,
            f"SSH key not found for {dep['id']}. "
            f"Looked for: {dep.get('ssh_key_path', 'N/A')}",
        )

    user = dep.get("ssh_user", "ubuntu")
    host = dep["host"]
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-i", ssh_key,
        f"{user}@{host}",
        "cd /opt/quantsystem && docker compose -f deploy/docker-compose.yml restart",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            # Invalidate cache
            _cache_timestamps.pop(dep["id"], None)
            return ActionResponse(
                status="ok",
                message=f"Restart command sent to {dep['name']}",
                data={"stdout": result.stdout, "stderr": result.stderr},
            )
        else:
            return ActionResponse(
                status="error",
                message=f"Restart failed: {result.stderr}",
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH command timed out")
    except Exception as e:
        raise HTTPException(502, f"SSH failed: {e}")


@router.get("/{deploy_id}/logs")
async def deployment_logs(deploy_id: str, lines: int = 50):
    """Fetch recent Docker logs from remote VPS via SSH."""
    dep = get_deployment(deploy_id)
    ssh_key = _find_ssh_key(dep)
    if not ssh_key:
        raise HTTPException(400, f"SSH key not found for {dep['id']}")

    user = dep.get("ssh_user", "ubuntu")
    host = dep["host"]
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-i", ssh_key,
        f"{user}@{host}",
        f"docker logs qs-live-trader --tail {min(lines, 200)} 2>&1",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        log_lines = result.stdout.strip().split("\n") if result.stdout else []
        return {
            "id": dep["id"],
            "lines": log_lines,
            "count": len(log_lines),
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH command timed out")
    except Exception as e:
        raise HTTPException(502, f"SSH failed: {e}")
