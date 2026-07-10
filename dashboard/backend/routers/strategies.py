"""Strategy management API: list, deploy, start, stop, adjust strategies.

Provides a unified strategy registry that:
  - Discovers all available strategies from the strategies/ folder
  - Tracks deployed strategies and their state (running/stopped/error)
  - Supports paper and live trading modes
  - Stores deployment config in data/strategy_deployments.json
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from core.paths import get_data_dir, get_env_path, get_logs_dir, get_resource_root  # noqa: E402

router = APIRouter()

PROJECT_ROOT = get_resource_root()
STATE_DIR = get_data_dir() / "live_state"
LOG_DIR = get_logs_dir() / "paper_trading"
DEPLOY_FILE = get_data_dir() / "strategy_deployments.json"
ENV_FILE = get_env_path()
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Schemas ────────────────────────────────────────────────────

class StrategyInfo(BaseModel):
    """Available strategy definition (loaded from config/strategy_registry.yaml)."""
    id: str
    name: str
    description: str
    asset_class: str
    category: str  # options, equity, crypto, portfolio
    symbols: List[str] = []
    default_params: Dict[str, Any] = {}
    backtest_sharpe: Optional[float] = None
    backtest_win_rate: Optional[float] = None
    backtest_trades: Optional[int] = None
    # Lifecycle (research → backtest/validated → paper → live)
    stage: str = "research"
    gate: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}
    links: Dict[str, Any] = {}
    notes: str = ""
    runnable: bool = False


class DeployedStrategy(BaseModel):
    """A strategy that has been deployed (running or stopped)."""
    id: str
    strategy_id: str
    name: str
    mode: str = "paper"  # "paper" | "live"
    status: str = "stopped"  # "running" | "stopped" | "error" | "paused"
    data_source: str = "yahoo"
    execution_broker: str = "internal"  # "internal" | "alpaca" | "ib"
    initial_capital: float = 100000.0
    deployed_at: str = ""
    last_run: Optional[str] = None
    error_message: Optional[str] = None
    # Runtime stats (from state file)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 100000.0
    total_trades: int = 0
    win_rate: Optional[float] = None
    current_position: Optional[str] = None
    spy_price: float = 0.0


class DeployRequest(BaseModel):
    """Request to deploy a strategy."""
    strategy_id: str
    mode: str = "paper"
    data_source: str = "alpaca"
    execution_broker: str = "internal"
    initial_capital: float = 100000.0
    params: Dict[str, Any] = {}


class AdjustRequest(BaseModel):
    """Request to adjust a deployed strategy's parameters."""
    mode: Optional[str] = None
    data_source: Optional[str] = None
    execution_broker: Optional[str] = None
    initial_capital: Optional[float] = None
    params: Dict[str, Any] = {}


# ── Strategy catalog: loaded from config/strategy_registry.yaml ──
# Single source of truth for the strategy lifecycle. No hardcoded
# entries here — edit the YAML, not this file.

_REGISTRY_PATH = PROJECT_ROOT / "config" / "strategy_registry.yaml"

STAGE_ORDER = ["live", "paper", "validated", "research", "retired", "rejected"]


def _load_registry() -> List[StrategyInfo]:
    import yaml
    raw = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8"))
    out: List[StrategyInfo] = []
    for s in raw.get("strategies", []):
        m = s.get("metrics") or {}
        out.append(StrategyInfo(
            id=s["id"],
            name=s["name"],
            description=" ".join(str(s.get("description", "")).split()),
            asset_class=s.get("asset_class", ""),
            category=s.get("category", ""),
            symbols=s.get("symbols", []),
            default_params=s.get("default_params") or {},
            backtest_sharpe=m.get("backtest_sharpe"),
            backtest_win_rate=m.get("win_rate"),
            backtest_trades=m.get("trades"),
            stage=s.get("stage", "research"),
            gate=s.get("gate") or {},
            metrics=m,
            links=s.get("links") or {},
            notes=" ".join(str(s.get("notes", "")).split()),
            runnable=bool(s.get("runnable", False)),
        ))
    return out


STRATEGY_CATALOG: List[StrategyInfo] = _load_registry()

CATALOG_MAP = {s.id: s for s in STRATEGY_CATALOG}

# ── State file mapping ──────────────────────────────────────────

STATE_FILE_MAP = {
    "spy_gamma_scalp": "gamma_scalp_state.pkl",
}

DAILY_LOG_MAP = {
    "spy_gamma_scalp": "gamma_scalp_daily.jsonl",
}


# ── Helpers ──────────────────────────────────────────────────────

def _load_deployments() -> List[Dict[str, Any]]:
    """Load deployed strategies from JSON file."""
    if not DEPLOY_FILE.exists():
        return []
    try:
        with open(DEPLOY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_deployments(deployments: List[Dict[str, Any]]) -> None:
    """Save deployed strategies to JSON file."""
    DEPLOY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_FILE, "w") as f:
        json.dump(deployments, f, indent=2, default=str)


def _load_strategy_state(strategy_id: str) -> Optional[Dict[str, Any]]:
    """Load strategy runtime state from pickle file."""
    filename = STATE_FILE_MAP.get(strategy_id)
    if not filename:
        return None
    state_file = STATE_DIR / filename
    if not state_file.exists():
        return None
    try:
        with open(state_file, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _load_daily_log(strategy_id: str, limit: int = 500) -> List[Dict]:
    """Load daily log for strategy."""
    filename = DAILY_LOG_MAP.get(strategy_id)
    if not filename:
        return []
    log_file = LOG_DIR / filename
    if not log_file.exists():
        return []
    lines = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return lines[-limit:]
    except Exception:
        return []


def _enrich_deployment(dep: Dict[str, Any]) -> DeployedStrategy:
    """Enrich a deployment record with runtime state data."""
    strategy_id = dep.get("strategy_id", "")
    state = _load_strategy_state(strategy_id)

    d = DeployedStrategy(**dep)

    if state:
        from dashboard.backend.routers.paper_trading import compute_unrealized_pnl
        d.realized_pnl = round(state.get("_running_pnl", 0), 2)
        d.total_trades = len(state.get("_trade_results", []))
        d.spy_price = round(state.get("_last_spot", 0), 2)

        initial = state.get("_initial_capital", d.initial_capital)
        running_pnl = state.get("_running_pnl", 0)
        unrealized = compute_unrealized_pnl(state)
        d.unrealized_pnl = round(unrealized, 2)
        d.total_equity = round(initial + running_pnl + unrealized, 2)

        if state.get("_straddle_open"):
            direction = state.get("_trade_direction", "none")
            contracts = state.get("_n_contracts", 0)
            d.current_position = f"{direction} {contracts}x straddle"
        else:
            d.current_position = "Flat"

        # Win rate from trade results
        results = state.get("_trade_results", [])
        if results:
            wins = sum(1 for r in results if r.get("total_pnl", 0) > 0)
            d.win_rate = round(wins / len(results) * 100, 1)

        # Last run time
        ct = state.get("_current_time")
        if ct:
            d.last_run = str(ct.date() if hasattr(ct, "date") else ct)

        d.status = "running"  # If state file exists, it's been running
    else:
        d.current_position = "Not started"

    return d


# ── Endpoints ────────────────────────────────────────────────────

@router.get("/registry")
async def get_strategy_registry():
    """Lifecycle view: strategies grouped by stage (live → paper → validated
    → research → retired → rejected), in pipeline order."""
    groups = {stage: [] for stage in STAGE_ORDER}
    for s in STRATEGY_CATALOG:
        groups.setdefault(s.stage, []).append(s.model_dump())
    return {
        "stages": STAGE_ORDER,
        "groups": groups,
        "gate_rule": "OOS Sharpe >= 1.5 or auto-reject",
        "counts": {stage: len(items) for stage, items in groups.items()},
    }


@router.get("/catalog", response_model=List[StrategyInfo])
async def get_strategy_catalog():
    """Get all available strategies that can be deployed."""
    return STRATEGY_CATALOG


@router.get("/catalog/{strategy_id}", response_model=StrategyInfo)
async def get_catalog_strategy(strategy_id: str):
    """Get a single strategy from the catalog by ID."""
    if strategy_id not in CATALOG_MAP:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found in catalog")
    return CATALOG_MAP[strategy_id]


@router.get("/", response_model=List[DeployedStrategy])
async def list_strategies():
    """List all deployed strategies with their current status."""
    deployments = _load_deployments()
    return [_enrich_deployment(d) for d in deployments]


@router.get("/{deploy_id}", response_model=DeployedStrategy)
async def get_strategy_detail(deploy_id: str):
    """Get detailed info about a deployed strategy."""
    deployments = _load_deployments()
    for d in deployments:
        if d.get("id") == deploy_id:
            return _enrich_deployment(d)
    raise HTTPException(status_code=404, detail=f"Deployment {deploy_id} not found")


@router.post("/deploy", response_model=DeployedStrategy)
async def deploy_strategy(req: DeployRequest):
    """Deploy a strategy for paper or live trading."""
    if req.strategy_id not in CATALOG_MAP:
        raise HTTPException(status_code=404, detail=f"Strategy {req.strategy_id} not found in catalog")

    catalog_entry = CATALOG_MAP[req.strategy_id]
    deployments = _load_deployments()

    # Check if already deployed
    for d in deployments:
        if d.get("strategy_id") == req.strategy_id and d.get("status") in ("running", "stopped"):
            raise HTTPException(status_code=409, detail=f"Strategy {req.strategy_id} is already deployed")

    deploy_id = f"{req.strategy_id}_{req.mode}_{int(datetime.now().timestamp())}"
    deployment = {
        "id": deploy_id,
        "strategy_id": req.strategy_id,
        "name": catalog_entry.name,
        "mode": req.mode,
        "status": "stopped",
        "data_source": req.data_source,
        "execution_broker": req.execution_broker,
        "initial_capital": req.initial_capital,
        "deployed_at": datetime.now().isoformat(),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_equity": req.initial_capital,
        "total_trades": 0,
        "spy_price": 0.0,
    }

    deployments.append(deployment)
    _save_deployments(deployments)

    return DeployedStrategy(**deployment)


@router.post("/{deploy_id}/start")
async def start_strategy(deploy_id: str, background_tasks: BackgroundTasks):
    """Start a deployed strategy."""
    deployments = _load_deployments()
    target = None
    for d in deployments:
        if d.get("id") == deploy_id:
            target = d
            break

    if not target:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if target.get("status") == "running":
        return {"status": "already_running", "deploy_id": deploy_id}

    target["status"] = "running"
    target["last_run"] = datetime.now().isoformat()
    _save_deployments(deployments)

    # Launch the paper trading script in background
    strategy_id = target.get("strategy_id", "")
    if strategy_id == "spy_gamma_scalp":
        def _run():
            python = sys.executable
            script = str(PROJECT_ROOT / "scripts" / "live_paper_trade.py")
            cmd = [
                python, script,
                "--data-source", target.get("data_source", "yahoo"),
                "--mode", "once",
                "--initial-capital", str(target.get("initial_capital", 100000)),
            ]
            broker = target.get("execution_broker", "internal")
            if broker == "alpaca":
                cmd.append("--alpaca-execution")
            elif broker == "ib":
                cmd.append("--ib-execution")
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except Exception:
                pass

        background_tasks.add_task(_run)

    return {"status": "started", "deploy_id": deploy_id}


@router.post("/{deploy_id}/stop")
async def stop_strategy(deploy_id: str):
    """Stop a deployed strategy."""
    deployments = _load_deployments()
    for d in deployments:
        if d.get("id") == deploy_id:
            d["status"] = "stopped"
            _save_deployments(deployments)
            return {"status": "stopped", "deploy_id": deploy_id}
    raise HTTPException(status_code=404, detail="Deployment not found")


@router.post("/{deploy_id}/adjust")
async def adjust_strategy(deploy_id: str, req: AdjustRequest):
    """Adjust parameters of a deployed strategy."""
    deployments = _load_deployments()
    for d in deployments:
        if d.get("id") == deploy_id:
            if req.mode is not None:
                d["mode"] = req.mode
            if req.data_source is not None:
                d["data_source"] = req.data_source
            if req.execution_broker is not None:
                d["execution_broker"] = req.execution_broker
            if req.initial_capital is not None:
                d["initial_capital"] = req.initial_capital
            _save_deployments(deployments)
            return {"status": "adjusted", "deploy_id": deploy_id, "changes": req.dict(exclude_none=True)}
    raise HTTPException(status_code=404, detail="Deployment not found")


@router.delete("/{deploy_id}")
async def remove_strategy(deploy_id: str):
    """Remove a deployed strategy (must be stopped first)."""
    deployments = _load_deployments()
    new_deps = []
    removed = False
    for d in deployments:
        if d.get("id") == deploy_id:
            if d.get("status") == "running":
                raise HTTPException(status_code=409, detail="Cannot remove a running strategy. Stop it first.")
            removed = True
        else:
            new_deps.append(d)

    if not removed:
        raise HTTPException(status_code=404, detail="Deployment not found")

    _save_deployments(new_deps)
    return {"status": "removed", "deploy_id": deploy_id}


@router.get("/{strategy_id}/signals")
async def get_strategy_signals(strategy_id: str, limit: int = 50):
    """Get recent signals from a strategy."""
    entries = _load_daily_log(strategy_id, limit)
    signals = []
    for e in entries:
        iv = e.get("implied_vol", 0)
        rv = e.get("rv_20d", 0)
        signals.append({
            "date": e.get("date", ""),
            "vrp": round(iv - rv, 4),
            "implied_vol": round(iv, 4),
            "rv_20d": round(rv, 4),
            "actions": e.get("actions", []),
            "position_open": e.get("position_open", False),
            "direction": e.get("trade_direction", "none"),
        })
    return signals


@router.get("/{deploy_id}/equity-curve")
async def get_deployed_equity_curve(deploy_id: str):
    """Get equity curve for a deployed strategy."""
    deployments = _load_deployments()
    strategy_id = None
    for d in deployments:
        if d.get("id") == deploy_id:
            strategy_id = d.get("strategy_id")
            break

    if not strategy_id:
        raise HTTPException(status_code=404, detail="Deployment not found")

    entries = _load_daily_log(strategy_id, 1000)
    if not entries:
        return []

    peak = 0.0
    result = []
    for e in entries:
        equity = e.get("total_equity", 100000)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0
        result.append({
            "date": e.get("date", ""),
            "equity": round(equity, 2),
            "drawdown": round(dd * 100, 2),
        })
    return result
