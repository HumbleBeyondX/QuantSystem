"""Risk API: multi-strategy risk monitoring with email alerts.

Provides:
  GET /summary?strategy_id=...         → risk metrics for a single strategy
  GET /strategies                      → list of strategies with risk overview
  GET /portfolio                       → cross-strategy budgets/exposures/stress
  GET /alerts/history                  → recent alert history
  POST /alerts/test-email              → send a test alert email
  POST /alerts/configure               → configure alert thresholds
  POST /circuit-breaker/reset          → manual circuit breaker reset
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


def _safe(v: float, default: float = 0.0) -> float:
    """Convert NaN/inf to a safe default."""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return default
    return v

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.logging import get_logger

logger = get_logger(__name__)

from core.paths import get_data_dir, get_logs_dir, get_resource_root  # noqa: E402

router = APIRouter()

PROJECT_ROOT = get_resource_root()
STATE_DIR = get_data_dir() / "live_state"
LOG_DIR = get_logs_dir() / "paper_trading"
ALERT_CONFIG_FILE = get_data_dir() / "alert_config.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Schemas ──────────────────────────────────────────────────────

class StrategyRiskOverview(BaseModel):
    """Minimal risk info per strategy for the selector."""
    strategy_id: str
    name: str
    asset_class: str
    status: str  # running / stopped / no_data
    equity: float = 0.0
    pnl: float = 0.0
    current_dd: float = 0.0
    max_dd: float = 0.0
    has_position: bool = False


class RiskSummary(BaseModel):
    """Full risk detail for a single strategy."""
    strategy_id: str
    name: str
    asset_class: str
    status: str

    # P&L
    equity: float = 0.0
    initial_capital: float = 100000.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    return_pct: float = 0.0
    total_trades: int = 0

    # Risk metrics
    var_95: float = 0.0
    cvar_95: float = 0.0
    current_dd: float = 0.0
    max_dd: float = 0.0
    gross_leverage: float = 0.0
    net_exposure: str = "Flat"

    # Greeks (options strategies only)
    portfolio_delta: float = 0.0
    portfolio_gamma: float = 0.0
    portfolio_theta: float = 0.0
    portfolio_vega: float = 0.0
    has_greeks: bool = False

    # Position
    position_open: bool = False
    position_detail: str = "Flat"
    consecutive_losses: int = 0

    # Market data
    spy_price: float = 0.0
    vix: float = 0.0
    implied_vol: float = 0.0
    rv_20d: float = 0.0
    vrp: float = 0.0

    # Circuit breakers
    circuit_breakers: List[Dict[str, Any]] = []

    # Drawdown timeline
    drawdown_timeline: List[Dict[str, Any]] = []

    # Equity curve
    equity_curve: List[Dict[str, Any]] = []


class AlertConfig(BaseModel):
    """Alert threshold configuration."""
    email_enabled: bool = True
    recipient: str = "humblebeyondx@gmail.com"
    # Thresholds
    drawdown_warning_pct: float = 5.0
    drawdown_critical_pct: float = 10.0
    daily_loss_warning_pct: float = 3.0
    daily_loss_critical_pct: float = 5.0
    vix_spike_threshold: float = 30.0
    consecutive_loss_threshold: int = 4
    leverage_warning: float = 3.0
    leverage_critical: float = 5.0


class AlertRecord(BaseModel):
    timestamp: str
    severity: str
    title: str
    message: str
    source: str
    metadata: Dict[str, Any] = {}


# ── Strategy definitions (what we can monitor) ───────────────────

MONITORED_STRATEGIES = [
    {
        "strategy_id": "spy_gamma_scalp",
        "name": "SPY Gamma Scalp",
        "asset_class": "options",
        "state_file": "gamma_scalp_state.pkl",
        "daily_log": "gamma_scalp_daily.jsonl",
        "has_greeks": True,
    },
    {
        "strategy_id": "crypto_macro_cycle",
        "name": "BTC/ETH Macro Cycle",
        "asset_class": "crypto",
        "state_file": "crypto_macro_state.pkl",
        "daily_log": "crypto_macro_daily.jsonl",
        "has_greeks": False,
    },
    {
        "strategy_id": "delta_neutral",
        "name": "Options Delta-Neutral",
        "asset_class": "options",
        "state_file": "delta_neutral_state.pkl",
        "daily_log": "delta_neutral_daily.jsonl",
        "has_greeks": True,
    },
    {
        "strategy_id": "equity_factor",
        "name": "Equity Factor Strategy",
        "asset_class": "equity",
        "state_file": "equity_factor_state.pkl",
        "daily_log": "equity_factor_daily.jsonl",
        "has_greeks": False,
    },
    {
        "strategy_id": "crypto_swing",
        "name": "Crypto Swing",
        "asset_class": "crypto",
        "state_file": "crypto_swing_state.pkl",
        "daily_log": "crypto_swing_daily.jsonl",
        "has_greeks": False,
    },
]

STRATEGY_MAP = {s["strategy_id"]: s for s in MONITORED_STRATEGIES}


# ── Alert state (in-memory, persisted to disk) ───────────────────

_alert_history: List[Dict[str, Any]] = []
_alert_config: Optional[AlertConfig] = None


def _load_alert_config() -> AlertConfig:
    global _alert_config
    if _alert_config is not None:
        return _alert_config
    if ALERT_CONFIG_FILE.exists():
        try:
            with open(ALERT_CONFIG_FILE) as f:
                data = json.load(f)
            _alert_config = AlertConfig(**data)
        except Exception:
            _alert_config = AlertConfig()
    else:
        _alert_config = AlertConfig()
    return _alert_config


def _save_alert_config(config: AlertConfig) -> None:
    global _alert_config
    _alert_config = config
    ALERT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_CONFIG_FILE, "w") as f:
        json.dump(config.dict(), f, indent=2)


# ── Helpers ──────────────────────────────────────────────────────

def _load_state(strategy_id: str) -> Optional[Dict[str, Any]]:
    """Load strategy state from pickle file."""
    info = STRATEGY_MAP.get(strategy_id)
    if not info:
        return None
    state_file = STATE_DIR / info["state_file"]
    if not state_file.exists():
        return None
    try:
        with open(state_file, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _load_jsonl(filepath: Path, limit: int = 500) -> List[Dict[str, Any]]:
    """Load last N lines from a JSONL file."""
    if not filepath.exists():
        return []
    lines = []
    try:
        with open(filepath) as f:
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


def _compute_equity_curve(strategy_id: str) -> List[Dict[str, Any]]:
    """Build equity curve from daily log."""
    info = STRATEGY_MAP.get(strategy_id)
    if not info:
        return []
    entries = _load_jsonl(LOG_DIR / info["daily_log"], 1000)
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


def _compute_var_cvar(equity_curve: List[Dict[str, Any]]) -> tuple[float, float, float]:
    """Compute VaR(95%), CVaR, and current equity from equity curve."""
    if len(equity_curve) < 5:
        eq = equity_curve[-1]["equity"] if equity_curve else 100000
        return 0.0, 0.0, eq

    equity = equity_curve[-1]["equity"]
    daily_returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]["equity"]
        if prev > 0:
            daily_returns.append((equity_curve[i]["equity"] - prev) / prev)

    if len(daily_returns) < 5:
        return 0.0, 0.0, equity

    daily_returns.sort()
    idx_5 = max(int(len(daily_returns) * 0.05), 1)
    var_95 = abs(daily_returns[idx_5 - 1]) * equity
    cvar_95 = abs(sum(daily_returns[:idx_5]) / idx_5) * equity
    return round(var_95, 2), round(cvar_95, 2), equity


def _build_risk_summary(strategy_id: str) -> RiskSummary:
    """Build full risk summary for a strategy."""
    info = STRATEGY_MAP.get(strategy_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    state = _load_state(strategy_id)
    eq_curve = _compute_equity_curve(strategy_id)

    # Base info
    summary = RiskSummary(
        strategy_id=strategy_id,
        name=info["name"],
        asset_class=info["asset_class"],
        has_greeks=info.get("has_greeks", False),
        status="no_data",
    )

    if not state and not eq_curve:
        return summary

    # State-based metrics
    if state:
        summary.status = "running"
        initial_capital = state.get("_initial_capital", 100000)
        running_pnl = state.get("_running_pnl", 0)

        # Compute unrealized PnL (options-specific)
        unrealized = 0.0
        if strategy_id == "spy_gamma_scalp":
            try:
                from dashboard.backend.routers.paper_trading import compute_unrealized_pnl
                unrealized = compute_unrealized_pnl(state)
            except Exception:
                pass
        elif state.get("_unrealized_pnl"):
            unrealized = state.get("_unrealized_pnl", 0)

        equity = initial_capital + running_pnl + unrealized
        peak = max(state.get("_peak_equity", equity), equity)
        current_dd = ((peak - equity) / peak * 100) if peak > 0 else 0

        summary.equity = round(equity, 2)
        summary.initial_capital = initial_capital
        summary.realized_pnl = round(running_pnl, 2)
        summary.unrealized_pnl = round(unrealized, 2)
        summary.total_pnl = round(running_pnl + unrealized, 2)
        summary.return_pct = round(((equity - initial_capital) / initial_capital) * 100, 2)
        summary.total_trades = len(state.get("_trade_results", []))
        summary.current_dd = round(current_dd, 2)
        summary.consecutive_losses = state.get("_consecutive_losses", 0)

        # Options Greeks
        if info.get("has_greeks", False):
            is_open = state.get("_straddle_open", False) or state.get("_position_open", False)
            summary.position_open = is_open
            if is_open:
                summary.portfolio_delta = round(state.get("_portfolio_delta", 0), 2)
                summary.portfolio_gamma = round(state.get("_portfolio_gamma", 0), 2)
                summary.portfolio_theta = round(state.get("_portfolio_theta", 0), 2)
                summary.portfolio_vega = round(state.get("_portfolio_vega", 0), 2)
                direction = state.get("_trade_direction", "none")
                contracts = state.get("_n_contracts", 0)
                strike = state.get("_strike", 0)
                summary.position_detail = f"{direction} {contracts}c @ ${strike}"
                summary.net_exposure = f"{summary.portfolio_delta:.1f} delta"
            else:
                summary.position_detail = "Flat"
                summary.net_exposure = "Flat (no position)"

            # Leverage for options
            spy_price = _safe(state.get("_last_spot", 0))
            contracts = state.get("_n_contracts", 0) if state.get("_straddle_open") else 0
            hedge_shares = _safe(state.get("_hedge_shares", 0)) if state.get("_straddle_open") else 0
            notional = abs(contracts * 100 * spy_price) + abs(hedge_shares * spy_price)
            summary.gross_leverage = _safe(round(notional / equity, 2)) if equity > 0 else 0.0
            summary.spy_price = round(spy_price, 2)
        else:
            # Crypto / equity position
            position = _safe(state.get("_position", 0))
            summary.position_open = abs(position) > 1e-12 if isinstance(position, float) else bool(position)
            if summary.position_open:
                entry = _safe(state.get("_entry_price", 0))
                summary.position_detail = f"{'Long' if position > 0 else 'Short'} @ ${entry:,.2f}"
                leverage = _safe(state.get("_leverage", 1.0))
                summary.gross_leverage = round(leverage, 2)
                summary.net_exposure = f"{'Long' if position > 0 else 'Short'} {leverage:.1f}x"
            else:
                summary.position_detail = "Flat"
                summary.net_exposure = "Flat"

        # Market data
        iv = _safe(state.get("_implied_vol", 0))
        rv = _safe(state.get("_rv_20d", 0))
        summary.vix = round(iv * 100, 2) if iv < 1 else round(iv, 2)
        summary.implied_vol = round(iv, 4) if iv < 1 else round(iv / 100, 4)
        summary.rv_20d = round(rv, 4)
        summary.vrp = round((iv if iv < 1 else iv / 100) - rv, 4)

    # Equity curve based metrics
    if eq_curve:
        var95, cvar95, _ = _compute_var_cvar(eq_curve)
        summary.var_95 = var95
        summary.cvar_95 = cvar95
        summary.max_dd = round(max(e["drawdown"] for e in eq_curve), 2)
        summary.equity_curve = eq_curve
        summary.drawdown_timeline = [{"date": e["date"], "dd": round(-e["drawdown"], 2)} for e in eq_curve]

    # Circuit breakers
    initial = summary.initial_capital
    config = _load_alert_config()

    max_daily_loss = initial * (config.daily_loss_critical_pct / 100)
    today_pnl = summary.unrealized_pnl + summary.realized_pnl
    daily_loss = abs(today_pnl) if today_pnl < 0 else 0
    daily_pct = (daily_loss / max_daily_loss * 100) if max_daily_loss > 0 else 0

    max_dd_threshold = config.drawdown_critical_pct
    dd_pct = (summary.max_dd / max_dd_threshold * 100) if max_dd_threshold > 0 else 0

    max_lev = config.leverage_critical
    lev_pct = _safe(summary.gross_leverage / max_lev * 100) if max_lev > 0 else 0

    max_consec = config.consecutive_loss_threshold
    consec_pct = (summary.consecutive_losses / max_consec * 100) if max_consec > 0 else 0

    def _cb_status(pct):
        if pct > 80: return "danger"
        if pct > 50: return "warning"
        return "ok"

    summary.circuit_breakers = [
        {"name": "Max Daily Loss", "threshold": f"${max_daily_loss:,.2f}", "current": f"${daily_loss:,.2f}",
         "status": _cb_status(daily_pct), "pct": min(daily_pct, 100)},
        {"name": "Max Drawdown", "threshold": f"{max_dd_threshold}%", "current": f"{summary.max_dd:.1f}%",
         "status": _cb_status(dd_pct), "pct": min(dd_pct, 100)},
        {"name": "Gross Leverage", "threshold": f"{max_lev:.1f}x", "current": f"{summary.gross_leverage:.2f}x",
         "status": _cb_status(lev_pct), "pct": min(lev_pct, 100)},
        {"name": "Consecutive Losses", "threshold": str(max_consec), "current": str(summary.consecutive_losses),
         "status": _cb_status(consec_pct), "pct": min(consec_pct, 100)},
    ]

    return summary


# ── Risk alert checking ──────────────────────────────────────────

async def _check_and_send_alerts(summary: RiskSummary) -> List[Dict[str, Any]]:
    """Check risk thresholds and send email alerts if breached."""
    config = _load_alert_config()
    if not config.email_enabled:
        return []

    triggered = []

    # Import email notifier
    try:
        from dashboard.alerts.email_notifier import get_email_notifier
        from dashboard.alerts.alert_manager import Alert, AlertSeverity
        notifier = get_email_notifier()
        if not notifier.is_configured:
            return []
    except ImportError:
        return []

    strategy_label = f"{summary.name} ({summary.strategy_id})"

    # 1. Drawdown alerts
    if summary.max_dd >= config.drawdown_critical_pct:
        alert = Alert(
            title=f"CRITICAL Drawdown: {summary.name}",
            message=f"Max drawdown has reached {summary.max_dd:.1f}% (threshold: {config.drawdown_critical_pct}%). "
                    f"Current equity: ${summary.equity:,.2f}, P&L: ${summary.total_pnl:,.2f}.",
            severity=AlertSeverity.CRITICAL,
            source=strategy_label,
            metadata={
                "strategy": summary.strategy_id,
                "max_dd": f"{summary.max_dd:.2f}%",
                "equity": f"${summary.equity:,.2f}",
                "position": summary.position_detail,
            },
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "critical", "title": alert.title})
    elif summary.max_dd >= config.drawdown_warning_pct:
        alert = Alert(
            title=f"Drawdown Warning: {summary.name}",
            message=f"Max drawdown at {summary.max_dd:.1f}% (warning: {config.drawdown_warning_pct}%, critical: {config.drawdown_critical_pct}%).",
            severity=AlertSeverity.WARNING,
            source=strategy_label,
            metadata={"strategy": summary.strategy_id, "max_dd": f"{summary.max_dd:.2f}%"},
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "warning", "title": alert.title})

    # 2. Daily loss alerts
    today_pnl = summary.unrealized_pnl + summary.realized_pnl
    daily_loss_pct = abs(today_pnl) / summary.initial_capital * 100 if today_pnl < 0 else 0
    if daily_loss_pct >= config.daily_loss_critical_pct:
        alert = Alert(
            title=f"CRITICAL Daily Loss: {summary.name}",
            message=f"Today's loss is {daily_loss_pct:.1f}% of capital (${abs(today_pnl):,.2f}). "
                    f"Threshold: {config.daily_loss_critical_pct}%.",
            severity=AlertSeverity.CRITICAL,
            source=strategy_label,
            metadata={"strategy": summary.strategy_id, "daily_loss": f"${abs(today_pnl):,.2f}", "pct": f"{daily_loss_pct:.1f}%"},
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "critical", "title": alert.title})
    elif daily_loss_pct >= config.daily_loss_warning_pct:
        alert = Alert(
            title=f"Daily Loss Warning: {summary.name}",
            message=f"Today's loss is {daily_loss_pct:.1f}% of capital.",
            severity=AlertSeverity.WARNING,
            source=strategy_label,
            metadata={"strategy": summary.strategy_id, "daily_loss": f"${abs(today_pnl):,.2f}"},
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "warning", "title": alert.title})

    # 3. VIX spike
    vix_value = summary.vix
    if vix_value >= config.vix_spike_threshold:
        alert = Alert(
            title=f"VIX Spike Alert",
            message=f"VIX has risen to {vix_value:.1f} (threshold: {config.vix_spike_threshold}). "
                    f"Consider reviewing {summary.name} position: {summary.position_detail}.",
            severity=AlertSeverity.WARNING,
            source=strategy_label,
            metadata={"vix": f"{vix_value:.1f}", "strategy": summary.strategy_id},
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "warning", "title": alert.title})

    # 4. Consecutive losses
    if summary.consecutive_losses >= config.consecutive_loss_threshold:
        alert = Alert(
            title=f"Consecutive Losses: {summary.name}",
            message=f"{summary.consecutive_losses} consecutive losing trades (threshold: {config.consecutive_loss_threshold}). "
                    f"Review strategy performance.",
            severity=AlertSeverity.WARNING,
            source=strategy_label,
            metadata={"consecutive_losses": summary.consecutive_losses, "strategy": summary.strategy_id},
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "warning", "title": alert.title})

    # 5. Leverage warning
    if summary.gross_leverage >= config.leverage_critical:
        alert = Alert(
            title=f"CRITICAL Leverage: {summary.name}",
            message=f"Gross leverage at {summary.gross_leverage:.2f}x (threshold: {config.leverage_critical}x).",
            severity=AlertSeverity.CRITICAL,
            source=strategy_label,
            metadata={"leverage": f"{summary.gross_leverage:.2f}x", "strategy": summary.strategy_id},
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "critical", "title": alert.title})
    elif summary.gross_leverage >= config.leverage_warning:
        alert = Alert(
            title=f"Leverage Warning: {summary.name}",
            message=f"Gross leverage at {summary.gross_leverage:.2f}x (warning: {config.leverage_warning}x).",
            severity=AlertSeverity.WARNING,
            source=strategy_label,
            metadata={"leverage": f"{summary.gross_leverage:.2f}x", "strategy": summary.strategy_id},
        )
        await notifier.send_alert(alert)
        triggered.append({"severity": "warning", "title": alert.title})

    # Store in history
    for t in triggered:
        _alert_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            **t,
            "strategy_id": summary.strategy_id,
        })
        # Keep history bounded
        if len(_alert_history) > 500:
            _alert_history.pop(0)

    return triggered


# ── Endpoints ────────────────────────────────────────────────────

@router.get("/portfolio")
async def get_portfolio_risk():
    """Cross-strategy portfolio risk: budgets, exposures, stress, flags.

    Aggregates the SPY gamma scalp + both BTC wheel paper books against
    config/risk_budgets.yaml. Fetches live Deribit greeks, so it runs in
    a thread; the report itself never raises (books degrade with an
    'error' field).
    """
    from core.risk.portfolio import portfolio_risk_report
    return await asyncio.to_thread(portfolio_risk_report)


@router.get("/strategies", response_model=List[StrategyRiskOverview])
async def list_risk_strategies():
    """List all monitored strategies with basic risk overview."""
    result = []
    for info in MONITORED_STRATEGIES:
        sid = info["strategy_id"]
        state = _load_state(sid)
        eq_curve = _compute_equity_curve(sid)

        if state:
            initial = state.get("_initial_capital", 100000)
            running_pnl = state.get("_running_pnl", 0)
            unrealized = 0.0
            if sid == "spy_gamma_scalp":
                try:
                    from dashboard.backend.routers.paper_trading import compute_unrealized_pnl
                    unrealized = compute_unrealized_pnl(state)
                except Exception:
                    pass
            equity = initial + running_pnl + unrealized
            peak = max(state.get("_peak_equity", equity), equity)
            current_dd = ((peak - equity) / peak * 100) if peak > 0 else 0
            max_dd = max(e["drawdown"] for e in eq_curve) if eq_curve else 0
            is_open = state.get("_straddle_open", False) or state.get("_position_open", False) or abs(state.get("_position", 0)) > 1e-12

            result.append(StrategyRiskOverview(
                strategy_id=sid,
                name=info["name"],
                asset_class=info["asset_class"],
                status="running",
                equity=round(equity, 2),
                pnl=round(running_pnl + unrealized, 2),
                current_dd=round(current_dd, 2),
                max_dd=round(max_dd, 2),
                has_position=is_open,
            ))
        else:
            result.append(StrategyRiskOverview(
                strategy_id=sid,
                name=info["name"],
                asset_class=info["asset_class"],
                status="no_data",
            ))
    return result


@router.get("/summary", response_model=RiskSummary)
async def get_risk_summary(strategy_id: str = Query(default="spy_gamma_scalp")):
    """Get detailed risk summary for a strategy. Also checks alert thresholds."""
    summary = _build_risk_summary(strategy_id)

    # Check alerts in background (don't block response)
    try:
        asyncio.create_task(_check_and_send_alerts(summary))
    except Exception:
        pass

    return summary


@router.get("/alerts/history", response_model=List[AlertRecord])
async def get_alert_history(limit: int = Query(default=50, ge=1, le=500)):
    """Get recent alert history."""
    records = _alert_history[-limit:]
    return [AlertRecord(
        timestamp=r.get("timestamp", ""),
        severity=r.get("severity", "info"),
        title=r.get("title", ""),
        message=r.get("message", ""),
        source=r.get("source", ""),
        metadata=r.get("metadata", {}),
    ) for r in reversed(records)]


@router.get("/alerts/config", response_model=AlertConfig)
async def get_alert_config():
    """Get current alert configuration."""
    return _load_alert_config()


@router.post("/alerts/configure", response_model=AlertConfig)
async def configure_alerts(config: AlertConfig):
    """Update alert thresholds and email settings."""
    _save_alert_config(config)
    return config


@router.post("/alerts/test-email")
async def test_email_alert():
    """Send a test alert email to verify configuration."""
    try:
        from dashboard.alerts.email_notifier import get_email_notifier
        from dashboard.alerts.alert_manager import Alert, AlertSeverity

        notifier = get_email_notifier()
        if not notifier.is_configured:
            return {
                "status": "error",
                "message": "Email not configured. Run: python -m dashboard.alerts.email_notifier --setup (OAuth2) "
                           "or set ALERT_EMAIL_SENDER + ALERT_EMAIL_PASSWORD in .env (App Password)",
                "auth_method": notifier.auth_method,
            }

        alert = Alert(
            title="Test Alert - QuantSystem",
            message="This is a test alert from your QuantSystem risk monitoring. If you received this email, your alert system is working correctly!",
            severity=AlertSeverity.INFO,
            source="risk_dashboard_test",
            metadata={
                "type": "test",
                "auth_method": notifier.auth_method,
                "timestamp": datetime.utcnow().isoformat(),
                "recipient": notifier.recipient,
            },
        )
        await notifier.send_alert(alert)
        return {"status": "sent", "recipient": notifier.recipient, "auth_method": notifier.auth_method}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/circuit-breaker/reset")
async def reset_circuit_breaker(strategy_id: str = Query(default="spy_gamma_scalp")):
    """Manually reset the circuit breaker for a strategy."""
    state = _load_state(strategy_id)
    if not state:
        raise HTTPException(status_code=404, detail="No state file found")

    # Reset circuit breaker flags
    state["_circuit_breaker_active"] = False
    state["_circuit_breaker"] = False
    state["_cb_bars"] = 0
    state["_peak_equity"] = state.get("_initial_capital", 100000) + state.get("_running_pnl", 0)

    # Save back
    info = STRATEGY_MAP.get(strategy_id)
    if info:
        state_file = STATE_DIR / info["state_file"]
        try:
            with open(state_file, "wb") as f:
                pickle.dump(state, f)
            return {"status": "reset", "strategy_id": strategy_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return {"status": "error", "message": "Strategy not found"}
