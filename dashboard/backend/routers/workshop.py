"""Strategy Workshop API: create, edit, test strategies from the UI or CLI.

Endpoints:
  - GET  /api/workshop/templates          — list available templates
  - GET  /api/workshop/strategies         — list all strategies with metadata
  - POST /api/workshop/scaffold           — create new strategy from template
  - GET  /api/workshop/code/{strategy_id} — read strategy source code
  - PUT  /api/workshop/code/{strategy_id} — update strategy source code
  - GET  /api/workshop/config/{strategy_id} — read YAML config
  - PUT  /api/workshop/config/{strategy_id} — update YAML config
  - POST /api/workshop/validate           — syntax-check Python code
  - POST /api/workshop/backtest           — quick backtest a strategy
  - GET  /api/workshop/backtest/{run_id}  — get backtest result
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from core.logging import get_logger

logger = get_logger(__name__)

from core.paths import get_resource_root  # noqa: E402

router = APIRouter()

# Workshop reads bundled templates / config / scripts.
PROJECT_ROOT = get_resource_root()

# In-memory backtest result store (replace with DB later)
_backtest_results: Dict[str, Dict[str, Any]] = {}


# ── Schemas ──────────────────────────────────────────────────────

class TemplateResponse(BaseModel):
    name: str
    description: str
    base_class: str
    asset_classes: List[str]
    default_symbols: List[str]
    default_params: Dict[str, Any]


class StrategyListItem(BaseModel):
    strategy_id: str
    class_name: str
    base_class: str
    category: str
    file_path: str
    description: str
    has_config: bool
    lines: int


class ScaffoldRequest(BaseModel):
    name: str
    template_type: str = "basic"
    symbols: Optional[List[str]] = None
    asset_class: Optional[str] = None
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


class ScaffoldResponse(BaseModel):
    strategy_id: str
    class_name: str
    template_type: str
    strategy_file: str
    config_file: str


class CodeResponse(BaseModel):
    strategy_id: str
    code: str
    file_path: str


class CodeUpdateRequest(BaseModel):
    code: str


class ConfigResponse(BaseModel):
    strategy_id: str
    config: str
    file_path: str


class ConfigUpdateRequest(BaseModel):
    config: str


class ValidateRequest(BaseModel):
    code: str


class ValidateResponse(BaseModel):
    valid: bool
    errors: List[str] = []
    warnings: List[str] = []
    class_name: Optional[str] = None
    base_class: Optional[str] = None
    has_generate_signals: bool = False


class BacktestRequest(BaseModel):
    strategy_id: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = 10000.0
    symbols: Optional[List[str]] = None


class BacktestStatusResponse(BaseModel):
    run_id: str
    status: str  # "pending" | "running" | "completed" | "failed"
    strategy_id: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ── Template endpoints ───────────────────────────────────────────

@router.get("/templates", response_model=List[TemplateResponse])
async def list_templates():
    """List all available strategy templates."""
    from strategies.scaffold import list_templates as _list

    templates = _list()
    return [
        TemplateResponse(
            name=t.name,
            description=t.description,
            base_class=t.base_class,
            asset_classes=t.asset_classes,
            default_symbols=t.default_symbols,
            default_params=t.default_params,
        )
        for t in templates
    ]


# ── Strategy CRUD ────────────────────────────────────────────────

@router.get("/strategies", response_model=List[StrategyListItem])
async def list_strategies():
    """List all existing strategies with metadata."""
    from strategies.scaffold import list_all_strategies

    strategies = list_all_strategies()
    return [StrategyListItem(**s) for s in strategies]


@router.post("/scaffold", response_model=ScaffoldResponse)
async def scaffold_strategy(req: ScaffoldRequest):
    """Create a new strategy from a template."""
    from strategies.scaffold import scaffold_strategy as _scaffold

    try:
        result = _scaffold(
            name=req.name,
            template_type=req.template_type,
            symbols=req.symbols,
            asset_class=req.asset_class,
            description=req.description,
            parameters=req.parameters,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))

    logger.info("strategy_scaffolded",
                strategy_id=result.strategy_id,
                template=result.template_type,
                file=result.strategy_file)

    return ScaffoldResponse(
        strategy_id=result.strategy_id,
        class_name=result.class_name,
        template_type=result.template_type,
        strategy_file=result.strategy_file,
        config_file=result.config_file,
    )


# ── Code read/write ──────────────────────────────────────────────

@router.get("/code/{strategy_id}", response_model=CodeResponse)
async def read_code(strategy_id: str):
    """Read the Python source code for a strategy."""
    from strategies.scaffold import find_strategy_file, read_strategy_file

    code = read_strategy_file(strategy_id)
    if code is None:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {strategy_id}")

    path = find_strategy_file(strategy_id)
    return CodeResponse(
        strategy_id=strategy_id,
        code=code,
        file_path=str(path) if path else "",
    )


@router.put("/code/{strategy_id}", response_model=CodeResponse)
async def update_code(strategy_id: str, req: CodeUpdateRequest):
    """Update the Python source code for a strategy."""
    from strategies.scaffold import write_strategy_file, find_strategy_file

    # Validate syntax first
    validation = _validate_python(req.code)
    if not validation["valid"]:
        raise HTTPException(status_code=400, detail={
            "message": "Invalid Python syntax",
            "errors": validation["errors"],
        })

    try:
        path = write_strategy_file(strategy_id, req.code)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {strategy_id}")

    logger.info("strategy_code_updated", strategy_id=strategy_id, file=path)

    return CodeResponse(
        strategy_id=strategy_id,
        code=req.code,
        file_path=path,
    )


# ── Config read/write ────────────────────────────────────────────

@router.get("/config/{strategy_id}", response_model=ConfigResponse)
async def read_config(strategy_id: str):
    """Read the YAML config for a strategy."""
    from strategies.scaffold import read_strategy_config

    config = read_strategy_config(strategy_id)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Config not found: {strategy_id}")

    config_path = PROJECT_ROOT / "config" / "strategies" / f"{strategy_id}.yaml"
    return ConfigResponse(
        strategy_id=strategy_id,
        config=config,
        file_path=str(config_path),
    )


@router.put("/config/{strategy_id}", response_model=ConfigResponse)
async def update_config(strategy_id: str, req: ConfigUpdateRequest):
    """Update the YAML config for a strategy."""
    from strategies.scaffold import write_strategy_config

    try:
        path = write_strategy_config(strategy_id, req.config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    logger.info("strategy_config_updated", strategy_id=strategy_id, file=path)

    return ConfigResponse(
        strategy_id=strategy_id,
        config=req.config,
        file_path=path,
    )


# ── Validation ───────────────────────────────────────────────────

def _validate_python(code: str) -> Dict[str, Any]:
    """Validate Python code: syntax, structure, required methods."""
    result: Dict[str, Any] = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "class_name": None,
        "base_class": None,
        "has_generate_signals": False,
    }

    # 1. Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        result["valid"] = False
        result["errors"].append(f"Line {e.lineno}: {e.msg}")
        return result

    # 2. Find strategy class
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = ""
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr

                valid_bases = {
                    "StrategyBase", "OptionsStrategyBase",
                    "FuturesStrategyBase", "HFTStrategyBase",
                }
                if base_name in valid_bases:
                    result["class_name"] = node.name
                    result["base_class"] = base_name

                    # Check for generate_signals
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if item.name == "generate_signals":
                                result["has_generate_signals"] = True

    # 3. Structure warnings
    if result["class_name"] is None:
        result["warnings"].append(
            "No strategy class found. Expected a class inheriting from "
            "StrategyBase, OptionsStrategyBase, FuturesStrategyBase, or HFTStrategyBase."
        )

    if not result["has_generate_signals"] and result["class_name"]:
        result["warnings"].append(
            f"Class {result['class_name']} does not define generate_signals(). "
            "This method is required for the strategy to produce trading signals."
        )

    # 4. Check for @register_strategy decorator
    has_register = "@register_strategy" in code
    if not has_register and result["class_name"]:
        result["warnings"].append(
            "Missing @register_strategy decorator. Add it to register the strategy "
            "in the global registry for discovery and deployment."
        )

    return result


@router.post("/validate", response_model=ValidateResponse)
async def validate_code(req: ValidateRequest):
    """Validate strategy Python code (syntax + structure)."""
    result = _validate_python(req.code)
    return ValidateResponse(**result)


# ── Quick backtest ───────────────────────────────────────────────

@router.post("/backtest", response_model=BacktestStatusResponse)
async def start_backtest(req: BacktestRequest, background_tasks: BackgroundTasks):
    """Launch a quick backtest for a strategy."""
    run_id = str(uuid.uuid4())[:8]

    _backtest_results[run_id] = {
        "run_id": run_id,
        "status": "pending",
        "strategy_id": req.strategy_id,
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "result": None,
        "error": None,
    }

    background_tasks.add_task(
        _run_backtest, run_id, req.strategy_id,
        req.start_date, req.end_date, req.initial_capital, req.symbols,
    )

    return BacktestStatusResponse(**_backtest_results[run_id])


@router.get("/backtest/{run_id}", response_model=BacktestStatusResponse)
async def get_backtest_result(run_id: str):
    """Get backtest status/result."""
    if run_id not in _backtest_results:
        raise HTTPException(status_code=404, detail=f"Backtest run not found: {run_id}")
    return BacktestStatusResponse(**_backtest_results[run_id])


async def _run_backtest(
    run_id: str,
    strategy_id: str,
    start_date: Optional[str],
    end_date: Optional[str],
    initial_capital: float,
    symbols: Optional[List[str]],
) -> None:
    """Execute a backtest in the background."""
    _backtest_results[run_id]["status"] = "running"

    try:
        # Build command: use the backtest module
        cmd = [
            sys.executable, "-m", "backtesting.run_backtest",
            "--strategy", strategy_id,
            "--capital", str(initial_capital),
        ]
        if start_date:
            cmd.extend(["--start", start_date])
        if end_date:
            cmd.extend(["--end", end_date])
        if symbols:
            cmd.extend(["--symbols"] + symbols)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode == 0:
            import json
            try:
                result = json.loads(stdout.decode())
            except Exception:
                result = {"raw_output": stdout.decode()[-2000:]}

            _backtest_results[run_id].update({
                "status": "completed",
                "completed_at": datetime.utcnow().isoformat(),
                "result": result,
            })
        else:
            _backtest_results[run_id].update({
                "status": "failed",
                "completed_at": datetime.utcnow().isoformat(),
                "error": stderr.decode()[-2000:],
            })

    except asyncio.TimeoutError:
        _backtest_results[run_id].update({
            "status": "failed",
            "completed_at": datetime.utcnow().isoformat(),
            "error": "Backtest timed out after 300 seconds",
        })
    except Exception as e:
        _backtest_results[run_id].update({
            "status": "failed",
            "completed_at": datetime.utcnow().isoformat(),
            "error": str(e),
        })
