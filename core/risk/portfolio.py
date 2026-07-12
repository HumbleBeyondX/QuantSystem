"""Policy-domain portfolio risk: the cross-strategy view nobody owns.

Three paper books run side by side — SPY gamma scalp (options on SPY)
and two BTC wheel variants (short strangles on Deribit) — each with its
own circuit breakers but NO aggregate vega or tail budget. A correlated
vol spike hits all of them at once. This module is that missing view:

  * collect_exposures()      — CURRENT positions across all books, with
                               real Deribit greeks for the wheel legs.
  * stress(exposures, scen)  — deterministic tail scenarios: BTC legs are
                               fully revalued with Black-Scholes, the SPY
                               book uses its delta/gamma/vega expansion.
  * portfolio_risk_report()  — exposures + budgets (config/risk_budgets.yaml)
                               + utilization + breaches + advisory flags.

v1 is ADVISORY: budget breaches surface {strategy: REDUCE_ONLY} flags via
GET /api/risk/portfolio and the Risk dashboard. Nothing is auto-blocked;
wiring the flags into the SLA-preflight gate is a deliberate follow-up.

Degradation doctrine: portfolio_risk_report() NEVER raises. Any book that
cannot be valued (Deribit unreachable, state file missing/corrupt) is
reported with an 'error' field and excluded from the aggregates — the
report says so instead of inventing numbers.
"""

from __future__ import annotations

import json
import math
import pickle
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml

from core.logging import get_logger
from core.paths import get_data_dir, get_resource_root

logger = get_logger(__name__)

BUDGETS_PATH = get_resource_root() / "config" / "risk_budgets.yaml"
STATE_DIR = get_data_dir() / "live_state"

GAMMA_STATE = STATE_DIR / "gamma_scalp_state.pkl"
WHEEL_STATES = {
    "btc_wheel": STATE_DIR / "btc_wheel_state.json",
    "btc_wheel_delta": STATE_DIR / "btc_wheel_delta_state.json",
}

# Deribit public API — tiny urllib helper, deliberately copied from
# scripts/wheel_paper_trade.py rather than imported (the script is an
# entrypoint with side-effectful CLI code, not a library).
DERIBIT_API = "https://www.deribit.com/api/v2/public"
DERIBIT_TIMEOUT = 10  # seconds; keeps the API endpoint bounded

# BTC options on Deribit are inverse instruments priced with r ~ 0;
# using 0 keeps our BS revaluation consistent with their mark IVs.
BTC_RISK_FREE = 0.0


def _deribit_get(path: str) -> dict:
    with urllib.request.urlopen(f"{DERIBIT_API}/{path}", timeout=DERIBIT_TIMEOUT) as r:
        return json.loads(r.read())["result"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Budgets ──────────────────────────────────────────────────────

def load_budgets() -> Dict[str, Any]:
    """Read config/risk_budgets.yaml (budgets + scenarios + action)."""
    raw = yaml.safe_load(BUDGETS_PATH.read_text(encoding="utf-8"))
    return raw or {}


# ── Exposure collection ──────────────────────────────────────────

@dataclass
class BookExposure:
    """One strategy book's contribution to the portfolio."""
    strategy_id: str
    asset: str                       # 'spy' | 'btc'
    equity_usd: float = 0.0
    position_open: bool = False
    vega_usd: float = 0.0            # USD P&L per +1 vol-point, signed (short vol < 0)
    delta_usd: float = 0.0           # USD P&L per +1% spot move
    short_vol_notional_usd: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def _wheel_exposure(strategy_id: str, state_path) -> BookExposure:
    """BTC wheel book: state JSON + REAL greeks from Deribit tickers.

    UNITS ASSUMPTION (verified live 2026-07-12 against
    BTC-17JUL26-58000-P: vega=5.69 with mark_price=0.0008 BTC):
    Deribit ticker `greeks` are per contract (1 BTC underlying) and
    quoted in USD — vega is the USD change in option value per +1
    absolute vol-point of IV, delta is per $1 of underlying. Option
    *prices* (mark_price) are quoted in BTC. Raw ticker greeks are
    stored in detail['legs'] so the assumption is auditable.
    """
    exp = BookExposure(strategy_id=strategy_id, asset="btc")
    try:
        st = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        exp.error = f"state file missing: {state_path.name}"
        return exp
    except Exception as e:  # corrupt json
        exp.error = f"state unreadable: {e}"
        return exp

    exp.equity_usd = float(st.get("equity", 0.0))
    pos = st.get("position")
    if not pos:
        return exp

    exp.position_open = True
    mult = float(pos.get("mult", 0.0))  # contracts per leg (BTC)
    try:
        index = float(_deribit_get("get_index_price?index_name=btc_usd")["index_price"])
        legs = []
        vega_sum = 0.0     # per-contract USD/vol-pt, both legs
        delta_sum = 0.0    # per-contract, per $1 of underlying
        for side in ("put", "call"):
            name = pos[side]
            t = _deribit_get(f"ticker?instrument_name={name}")
            g = t.get("greeks", {}) or {}
            legs.append({
                "instrument": name,
                "side": side,
                "strike": float(pos["kp" if side == "put" else "kc"]),
                "mark_price_btc": float(t.get("mark_price", 0.0)),
                "mark_price_usd": float(t.get("mark_price", 0.0)) * index,
                "mark_iv": float(t.get("mark_iv", 0.0)),   # in vol points, e.g. 50.19
                "greeks_raw": g,                           # raw Deribit values, auditable
            })
            vega_sum += float(g.get("vega", 0.0))
            delta_sum += float(g.get("delta", 0.0))

        # Short both legs → portfolio vega is minus the summed leg vegas.
        exp.vega_usd = -(vega_sum) * mult
        # USD P&L per +1% spot move: -delta * mult * (index * 1%).
        exp.delta_usd = -(delta_sum) * mult * index * 0.01
        # Short-premium underlying notional: one strangle controls `mult` BTC.
        exp.short_vol_notional_usd = mult * index
        exp.detail = {
            "index_usd": index,
            "mult": mult,
            "expiry_ms": pos.get("expiry_ms"),
            "entry_index": pos.get("entry_index"),
            "credit_total_usd": pos.get("credit_total"),
            "legs": legs,
        }
    except Exception as e:
        exp.error = f"deribit unreachable: {e}"
        logger.warning("portfolio risk: %s greeks fetch failed: %s", strategy_id, e)
    return exp


def _gamma_scalp_exposure() -> BookExposure:
    """SPY gamma scalp book from data/live_state/gamma_scalp_state.pkl.

    Reuses the same state keys the risk router reads (_portfolio_delta /
    _gamma / _theta / _vega, _straddle_open, ...). The strategy's
    _portfolio_vega is already USD per +1 vol-point (bs_vega is per 1%
    move, times contracts*100, signed by direction). Greeks are only
    trusted while a straddle is open — the state keeps stale values
    after a close.
    """
    exp = BookExposure(strategy_id="spy_gamma_scalp", asset="spy")
    try:
        with open(GAMMA_STATE, "rb") as f:
            st = pickle.load(f)
    except FileNotFoundError:
        exp.error = f"state file missing: {GAMMA_STATE.name}"
        return exp
    except Exception as e:
        exp.error = f"state unreadable: {e}"
        return exp

    initial = float(st.get("_initial_capital", 100_000))
    running = float(st.get("_running_pnl", 0.0))
    unrealized = _gamma_unrealized(st)
    exp.equity_usd = initial + running + unrealized

    is_open = bool(st.get("_straddle_open", False)) and st.get("_n_contracts", 0) > 0
    exp.position_open = is_open
    spot = float(st.get("_last_spot", 0.0) or 0.0)
    iv = float(st.get("_implied_vol", 0.0) or 0.0)
    direction = st.get("_trade_direction", "neutral")
    if is_open:
        delta_sh = float(st.get("_portfolio_delta", 0.0))   # share-equivalent
        exp.vega_usd = float(st.get("_portfolio_vega", 0.0))
        exp.delta_usd = delta_sh * spot * 0.01
        contracts = int(st.get("_n_contracts", 0))
        if direction == "sell_vol":
            exp.short_vol_notional_usd = contracts * 100 * spot
        exp.detail = {
            "direction": direction,
            "n_contracts": contracts,
            "strike": st.get("_strike"),
            "portfolio_delta_shares": delta_sh,
            "portfolio_gamma": float(st.get("_portfolio_gamma", 0.0)),
            "portfolio_theta_usd_day": float(st.get("_portfolio_theta", 0.0)),
            "spot": spot,
            "implied_vol": iv,
        }
    else:
        exp.detail = {"direction": direction, "spot": spot, "implied_vol": iv}
    return exp


def _gamma_unrealized(st: Dict[str, Any]) -> float:
    """Unrealized P&L for the open straddle (mirrors the paper router's
    compute_unrealized_pnl; reimplemented so core does not import the
    dashboard layer)."""
    if not st.get("_straddle_open") or st.get("_n_contracts", 0) == 0:
        return 0.0
    try:
        from strategies.options.gamma_scalp_strategy import bs_straddle
        spot = st.get("_last_spot", 0)
        strike = st.get("_strike", 0)
        iv = st.get("_implied_vol", 0.2)
        rf = st.get("_risk_free", 0.045)
        expiry, current = st.get("_expiry_time"), st.get("_current_time", datetime.now())
        # normalize tz-aware -> naive (same guard as the paper router)
        if expiry is not None and getattr(expiry, "tzinfo", None) is not None:
            expiry = expiry.replace(tzinfo=None)
        if current is not None and getattr(current, "tzinfo", None) is not None:
            current = current.replace(tzinfo=None)
        T = (max((expiry - current).total_seconds(), 60) / (365.25 * 86400)
             if expiry and current else 5 / 365.0)
        cp, pp = bs_straddle(spot, strike, T, rf, iv)
        n = st.get("_n_contracts", 0) * 100
        sign = -1.0 if st.get("_trade_direction") == "sell_vol" else 1.0
        pnl = ((cp + pp) - (st.get("_entry_call_price", 0) + st.get("_entry_put_price", 0))) * n * sign
        return pnl + st.get("_cum_hedge_pnl", 0) - st.get("_cum_txn_costs", 0)
    except Exception:
        return 0.0


def collect_exposures() -> Dict[str, Any]:
    """Gather CURRENT positions from every monitored book.

    Never raises: each book degrades independently to an 'error' entry.
    Aggregates only include cleanly-valued books.
    """
    books: List[BookExposure] = []
    try:
        books.append(_gamma_scalp_exposure())
    except Exception as e:  # belt-and-braces; the fn already catches
        books.append(BookExposure("spy_gamma_scalp", "spy", error=str(e)))
    for sid, path in WHEEL_STATES.items():
        try:
            books.append(_wheel_exposure(sid, path))
        except Exception as e:
            books.append(BookExposure(sid, "btc", error=str(e)))

    combined_equity = sum(b.equity_usd for b in books if b.error is None or b.equity_usd)
    net_vega = sum(b.vega_usd for b in books if b.error is None)
    notional = sum(b.short_vol_notional_usd for b in books if b.error is None)
    return {
        "generated_at": _now_iso(),
        "strategies": {b.strategy_id: asdict(b) for b in books},
        "combined_equity_usd": round(combined_equity, 2),
        "net_vega_usd": round(net_vega, 2),
        "abs_vega_usd": round(abs(net_vega), 2),
        "short_vol_notional_usd": round(notional, 2),
        "short_vol_notional_pct": round(notional / combined_equity, 4) if combined_equity > 0 else 0.0,
        "degraded": [b.strategy_id for b in books if b.error],
    }


# ── Stress ───────────────────────────────────────────────────────

def stress(exposures: Dict[str, Any], scenarios: Dict[str, Any]) -> Dict[str, Any]:
    """Revalue the portfolio under each scenario.

    BTC wheel legs: full Black-Scholes revaluation at shocked spot and
    shocked IV (leg mark IV + vol_shock_pts), T from the position's
    expiry_ms. SPY gamma scalp: linear-quadratic expansion
    pnl = delta*dS + 0.5*gamma*dS^2 + vega*dVolPts (no strike/expiry in
    a portfolio-greeks state → expansion is the honest option).
    Scenarios are single-domain (see config comment): a scenario shocks
    only the books whose asset matches its domain.
    """
    equity = exposures.get("combined_equity_usd", 0.0)
    out: Dict[str, Any] = {}
    for name, sc in (scenarios or {}).items():
        domain = sc.get("domain")
        per_strategy: Dict[str, float] = {}
        errors: List[str] = []
        for sid, b in exposures.get("strategies", {}).items():
            if b.get("error") or not b.get("position_open") or b.get("asset") != domain:
                continue
            try:
                if domain == "btc":
                    pnl = _stress_wheel(b, sc)
                elif domain == "spy":
                    pnl = _stress_gamma(b, sc)
                else:
                    continue
                per_strategy[sid] = round(float(pnl), 2)  # np.float64 -> float
            except Exception as e:
                errors.append(f"{sid}: {e}")
        total_pnl = sum(per_strategy.values())
        loss = max(0.0, -total_pnl)
        out[name] = {
            "description": sc.get("description", name),
            "pnl_usd": round(total_pnl, 2),
            "loss_usd": round(loss, 2),
            "loss_pct": round(loss / equity, 4) if equity > 0 else 0.0,
            "per_strategy_pnl_usd": per_strategy,
            "errors": errors,
        }
    return out


def _stress_wheel(b: Dict[str, Any], sc: Dict[str, Any]) -> float:
    """Full BS revaluation of the short strangle under the shock. USD P&L."""
    from strategies.options.gamma_scalp_strategy import bs_call, bs_put
    d = b["detail"]
    index = float(d["index_usd"])
    mult = float(d["mult"])
    expiry_ms = float(d["expiry_ms"])
    now_s = datetime.now(timezone.utc).timestamp()
    T = max(expiry_ms / 1000.0 - now_s, 3600) / (365.25 * 86400)
    s_shocked = index * (1.0 + float(sc.get("spot_shock", 0.0)))
    vol_pts = float(sc.get("vol_shock_pts", 0.0))
    pnl = 0.0
    for leg in d["legs"]:
        iv = max(leg["mark_iv"] / 100.0 + vol_pts / 100.0, 0.01)
        fn = bs_put if leg["side"] == "put" else bs_call
        shocked = fn(s_shocked, leg["strike"], T, BTC_RISK_FREE, iv)
        # short leg: we gain when the option cheapens
        pnl += (leg["mark_price_usd"] - shocked) * mult
    return pnl


def _stress_gamma(b: Dict[str, Any], sc: Dict[str, Any]) -> float:
    """Delta/gamma/vega expansion of the SPY book under the shock. USD P&L."""
    d = b["detail"]
    spot = float(d.get("spot", 0.0))
    iv = float(d.get("implied_vol", 0.0))
    dS = spot * float(sc.get("spot_shock", 0.0))
    vol_mult = float(sc.get("vol_multiplier", 1.0))
    d_vol_pts = iv * 100.0 * (vol_mult - 1.0)  # e.g. IV 15% doubling → +15 pts
    delta_sh = float(d.get("portfolio_delta_shares", 0.0))
    gamma = float(d.get("portfolio_gamma", 0.0))
    vega = float(b.get("vega_usd", 0.0))
    return delta_sh * dS + 0.5 * gamma * dS * dS + vega * d_vol_pts


# ── Report ───────────────────────────────────────────────────────

def portfolio_risk_report() -> Dict[str, Any]:
    """Exposures + budgets + utilization + breaches + advisory flags.

    Never raises. Budget checks:
      * abs_vega     — |net vega| vs max_abs_vega_usd
      * notional     — short-vol notional / combined equity vs max pct
      * stress:<name>— scenario loss_pct vs max_stress_loss_pct
    Breach attribution (advisory REDUCE_ONLY): the strategy contributing
    most to the breached metric.
    """
    try:
        exposures = collect_exposures()
    except Exception as e:  # truly unexpected — still return a report
        logger.exception("portfolio risk: exposure collection failed")
        return {"generated_at": _now_iso(), "error": str(e),
                "budgets": {}, "exposures": {}, "stress": {},
                "utilization": [], "breaches": [], "flags": {}, "advisory": True}

    try:
        cfg = load_budgets()
    except Exception as e:
        logger.exception("portfolio risk: budgets unreadable")
        return {"generated_at": exposures["generated_at"], "error": f"budgets unreadable: {e}",
                "budgets": {}, "exposures": exposures, "stress": {},
                "utilization": [], "breaches": [], "flags": {}, "advisory": True}

    budgets = cfg.get("budgets", {})
    scenarios = cfg.get("scenarios", {})
    stress_out = stress(exposures, scenarios)
    strategies = exposures.get("strategies", {})

    def _finite(x: float) -> float:
        return x if math.isfinite(x) else 0.0

    utilization: List[Dict[str, Any]] = []
    breaches: List[str] = []
    flags: Dict[str, str] = {}

    def _add(bid: str, label: str, used: float, limit: float, unit: str,
             offender: Optional[str]) -> None:
        used, limit = _finite(float(used)), _finite(float(limit))
        util = used / limit if limit > 0 else 0.0
        breached = bool(limit > 0 and used > limit)
        utilization.append({
            "id": bid, "label": label, "used": round(used, 4),
            "limit": limit, "unit": unit,
            "utilization": round(_finite(util), 4), "breached": breached,
        })
        if breached:
            breaches.append(bid)
            if offender:
                flags[offender] = "REDUCE_ONLY"

    # 1. Aggregate vega
    max_vega = float(budgets.get("max_abs_vega_usd", 0) or 0)
    vega_offender = max(
        (sid for sid, b in strategies.items() if not b.get("error")),
        key=lambda sid: abs(strategies[sid].get("vega_usd", 0.0)), default=None)
    _add("abs_vega", "Portfolio |vega|", exposures.get("abs_vega_usd", 0.0),
         max_vega, "$ / vol-pt", vega_offender)

    # 2. Short-vol notional
    max_pct = float(budgets.get("max_short_vol_notional_pct", 0) or 0)
    notl_offender = max(
        (sid for sid, b in strategies.items() if not b.get("error")),
        key=lambda sid: strategies[sid].get("short_vol_notional_usd", 0.0), default=None)
    _add("short_vol_notional", "Short-vol notional / equity",
         exposures.get("short_vol_notional_pct", 0.0), max_pct, "x equity", notl_offender)

    # 3. Stress scenarios
    max_stress = float(budgets.get("max_stress_loss_pct", 0) or 0)
    for name, res in stress_out.items():
        contrib = res.get("per_strategy_pnl_usd", {})
        offender = min(contrib, key=lambda s: contrib[s], default=None)  # worst loss
        _add(f"stress:{name}", f"Stress {name}", res.get("loss_pct", 0.0),
             max_stress, "of equity", offender)

    return {
        "generated_at": exposures["generated_at"],
        "advisory": True,
        "action_on_breach": cfg.get("action", "reduce_only"),
        "combined_equity_usd": exposures.get("combined_equity_usd", 0.0),
        "budgets": budgets,
        "exposures": exposures,
        "stress": stress_out,
        "utilization": utilization,
        "breaches": breaches,
        "flags": flags,
        "degraded": exposures.get("degraded", []),
    }


if __name__ == "__main__":
    print(json.dumps(portfolio_risk_report(), indent=2, default=str))
