"""Policy-domain portfolio risk: the cross-strategy view nobody owns.

Six paper books run side by side — SPY gamma scalp (options on SPY),
two BTC wheel variants (short strangles on Deribit), and three
tokenized-stock perp sleeves (trend scalp, funding carry, market
making) — each with its own circuit breakers but NO aggregate tail or
leverage budget. A correlated risk-off event hits all of them at once.
This module is that missing view, built around a UNIVERSAL book
contract that stays meaningful for ANY strategy type (options selling,
trend scalp, market making, funding carry, future HFT):

  * collect_exposures()      — CURRENT positions across all books, each
                               reporting the universal schema (equity,
                               day P&L, gross/net notional, leverage,
                               rails, freshness) plus a type-specific
                               `detail` dict (greeks/legs for options,
                               positions for linear, inventory for mm,
                               pair+funding for carry).
  * stress(exposures, scen)  — deterministic tail scenarios: BTC legs are
                               fully revalued with Black-Scholes, the SPY
                               book uses its delta/gamma/vega expansion,
                               linear/mm/carry books are first-order
                               (net_notional x spot_shock).
  * portfolio_risk_report()  — exposures + budgets (config/risk_budgets.yaml)
                               + utilization + breaches + advisory flags
                               + 30-day equity curves per book & combined.

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
from core.paths import get_data_dir, get_logs_dir, get_resource_root

logger = get_logger(__name__)

BUDGETS_PATH = get_resource_root() / "config" / "risk_budgets.yaml"
STATE_DIR = get_data_dir() / "live_state"
PAPER_LOGS_DIR = get_logs_dir() / "paper_trading"

GAMMA_STATE = STATE_DIR / "gamma_scalp_state.pkl"
WHEEL_STATES = {
    "btc_wheel": STATE_DIR / "btc_wheel_state.json",
    "btc_wheel_delta": STATE_DIR / "btc_wheel_delta_state.json",
}
SCALP_STATE = STATE_DIR / "scalp_sleeve_state.json"
CARRY_STATE = STATE_DIR / "funding_carry_state.json"
MM_STATE = STATE_DIR / "mm_sleeve_state.json"

# Every book's daily telemetry jsonl (logs/paper_trading/). Used for
# day P&L (last row vs previous day's), freshness, and equity curves.
BOOK_DAILY_LOGS = {
    "spy_gamma_scalp": "gamma_scalp_daily.jsonl",
    "btc_wheel": "btc_wheel_daily.jsonl",
    "btc_wheel_delta": "btc_wheel_delta_daily.jsonl",
    "scalp_sleeve": "scalp_sleeve_daily.jsonl",
    "funding_carry": "funding_carry_daily.jsonl",
    "mm_sleeve": "mm_sleeve_daily.jsonl",
}

# Freshness thresholds: runner books loop intraday (state heartbeats),
# daily books only write once per trading day.
FRESH_MAX_AGE_H_RUNNER = 2.0
FRESH_MAX_AGE_H_DAILY = 30.0

# Tokenized sleeves' shared daily-loss circuit breaker (mirrors the
# runners' own halt logic; read from state, cap declared here).
SLEEVE_DAILY_LOSS_CAP_PCT = 0.03

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


# ── Daily telemetry helpers (jsonl) ──────────────────────────────

def _read_daily_rows(strategy_id: str, max_rows: int = 120) -> List[dict]:
    """Tail of a book's daily jsonl. Never raises; skips corrupt lines."""
    fname = BOOK_DAILY_LOGS.get(strategy_id)
    if not fname:
        return []
    try:
        lines = (PAPER_LOGS_DIR / fname).read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return []
    rows: List[dict] = []
    for ln in lines[-max_rows:]:
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def _row_date(r: dict) -> str:
    return str(r.get("date") or str(r.get("ts") or r.get("logged_at") or "")[:10])


def _parse_ts(s: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(s))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _day_pnl_from_log(rows: List[dict], fallback: Optional[float] = None) -> float:
    """Last daily row's total_equity minus the previous DAY's total_equity.

    Multiple same-date rows collapse to the latest; with no prior day the
    fallback (e.g. the runner's own realized_today) is used, else 0.
    """
    last_eq: Optional[float] = None
    last_date = ""
    prev_eq: Optional[float] = None
    for r in reversed(rows):
        eq = r.get("total_equity")
        if eq is None:
            continue
        if last_eq is None:
            last_eq, last_date = float(eq), _row_date(r)
        elif _row_date(r) < last_date:
            prev_eq = float(eq)
            break
    if last_eq is None or prev_eq is None:
        return round(float(fallback), 2) if fallback is not None else 0.0
    return round(last_eq - prev_eq, 2)


def _freshness(state_path, rows: List[dict], max_age_h: float) -> Dict[str, Any]:
    """Last telemetry timestamp (state mtime vs last jsonl row) + fresh bool."""
    candidates: List[datetime] = []
    try:
        if state_path is not None and state_path.exists():
            candidates.append(
                datetime.fromtimestamp(state_path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        pass
    if rows:
        ts = _parse_ts(rows[-1].get("logged_at") or rows[-1].get("ts")
                       or rows[-1].get("timestamp"))
        if ts:
            candidates.append(ts)
    if not candidates:
        return {"last_telemetry": None, "age_hours": None, "fresh": False}
    last = max(candidates)
    age_h = max(0.0, (datetime.now(timezone.utc) - last).total_seconds() / 3600.0)
    return {
        "last_telemetry": last.isoformat(timespec="seconds"),
        "age_hours": round(age_h, 2),
        "fresh": age_h < max_age_h,
    }


def _rails(cap_pct: Optional[float] = None, realized_today: Optional[float] = None,
           equity: Optional[float] = None, halted: Optional[bool] = None) -> Dict[str, Any]:
    """Universal circuit-breaker snapshot: {cap, % of cap used, halted}."""
    used: Optional[float] = None
    if cap_pct and equity and realized_today is not None and equity > 0:
        used = round(max(0.0, -float(realized_today)) / (cap_pct * equity), 4)
    return {"daily_loss_cap_pct": cap_pct, "daily_used_pct": used, "halted": halted}


# ── Exposure collection ──────────────────────────────────────────

@dataclass
class BookExposure:
    """One strategy book's contribution to the portfolio.

    UNIVERSAL schema — every field above `detail` must stay meaningful
    for ANY strategy type. Type-specific facts (greeks, legs, inventory,
    funding) live in `detail` only.
    """
    strategy_id: str
    book_type: str                   # 'options' | 'linear' | 'mm' | 'carry'
    asset: str                       # 'spy' | 'btc' | 'tokenized'
    equity_usd: float = 0.0
    day_pnl_usd: float = 0.0
    gross_notional_usd: float = 0.0  # Σ|position notional|
    net_notional_usd: float = 0.0    # signed; options books use delta notional
    leverage: float = 0.0            # gross / equity
    position_open: bool = False
    vega_usd: float = 0.0            # USD P&L per +1 vol-point, signed (short vol < 0)
    delta_usd: float = 0.0           # USD P&L per +1% spot move
    short_vol_notional_usd: float = 0.0
    rails: Dict[str, Any] = field(default_factory=_rails)
    freshness: Dict[str, Any] = field(
        default_factory=lambda: {"last_telemetry": None, "age_hours": None, "fresh": False})
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def finalize(self) -> "BookExposure":
        """Derive leverage; round the universal money fields."""
        self.leverage = round(self.gross_notional_usd / self.equity_usd, 4) \
            if self.equity_usd > 0 else 0.0
        for f in ("equity_usd", "gross_notional_usd", "net_notional_usd"):
            setattr(self, f, round(float(getattr(self, f)), 2))
        return self


def _load_json_state(exp: BookExposure, state_path) -> Optional[dict]:
    """Shared state-file loader: sets exp.error and returns None on failure."""
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        exp.error = f"state file missing: {state_path.name}"
    except Exception as e:  # corrupt json
        exp.error = f"state unreadable: {e}"
    return None


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
    exp = BookExposure(strategy_id=strategy_id, book_type="options", asset="btc")
    rows = _read_daily_rows(strategy_id)
    exp.freshness = _freshness(state_path, rows, FRESH_MAX_AGE_H_DAILY)
    st = _load_json_state(exp, state_path)
    if st is None:
        return exp

    exp.equity_usd = float(st.get("equity", 0.0))
    exp.day_pnl_usd = _day_pnl_from_log(rows)
    pos = st.get("position")
    if not pos:
        return exp.finalize()

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
        exp.gross_notional_usd = exp.short_vol_notional_usd
        exp.net_notional_usd = exp.delta_usd * 100.0  # delta notional (USD per 100% move)
        exp.detail = {
            "index_usd": index,
            "mult": mult,
            "expiry_ms": pos.get("expiry_ms"),
            "entry_index": pos.get("entry_index"),
            "credit_total_usd": pos.get("credit_total"),
            "greeks": {"vega_usd": round(exp.vega_usd, 2),
                       "delta_usd_per_1pct": round(exp.delta_usd, 2)},
            "legs": legs,
        }
    except Exception as e:
        exp.error = f"deribit unreachable: {e}"
        logger.warning("portfolio risk: %s greeks fetch failed: %s", strategy_id, e)
    return exp.finalize()


def _gamma_scalp_exposure() -> BookExposure:
    """SPY gamma scalp book from data/live_state/gamma_scalp_state.pkl.

    Reuses the same state keys the risk router reads (_portfolio_delta /
    _gamma / _theta / _vega, _straddle_open, ...). The strategy's
    _portfolio_vega is already USD per +1 vol-point (bs_vega is per 1%
    move, times contracts*100, signed by direction). Greeks are only
    trusted while a straddle is open — the state keeps stale values
    after a close.
    """
    exp = BookExposure(strategy_id="spy_gamma_scalp", book_type="options", asset="spy")
    rows = _read_daily_rows("spy_gamma_scalp")
    exp.freshness = _freshness(GAMMA_STATE, rows, FRESH_MAX_AGE_H_DAILY)
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
    exp.day_pnl_usd = _day_pnl_from_log(rows)

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
        exp.gross_notional_usd = contracts * 100 * spot
        exp.net_notional_usd = exp.delta_usd * 100.0  # delta notional
        if direction == "sell_vol":
            exp.short_vol_notional_usd = contracts * 100 * spot
        exp.detail = {
            "direction": direction,
            "n_contracts": contracts,
            "strike": st.get("_strike"),
            "greeks": {
                "portfolio_delta_shares": delta_sh,
                "portfolio_gamma": float(st.get("_portfolio_gamma", 0.0)),
                "portfolio_theta_usd_day": float(st.get("_portfolio_theta", 0.0)),
                "vega_usd": exp.vega_usd,
            },
            # kept flat too — stress()'s expansion reads these directly
            "portfolio_delta_shares": delta_sh,
            "portfolio_gamma": float(st.get("_portfolio_gamma", 0.0)),
            "portfolio_theta_usd_day": float(st.get("_portfolio_theta", 0.0)),
            "spot": spot,
            "implied_vol": iv,
        }
    else:
        exp.detail = {"direction": direction, "spot": spot, "implied_vol": iv}
    return exp.finalize()


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


def _scalp_sleeve_exposure() -> BookExposure:
    """Tokenized-stock trend scalp (linear perps, 20x sleeve).

    State: data/live_state/scalp_sleeve_state.json written by the shadow
    runner (equity, positions{sym: {dir, entry, qty, ...}}, halted,
    realized_today). Gross = Σ|qty×entry|, net = Σ dir×qty×entry.
    """
    exp = BookExposure(strategy_id="scalp_sleeve", book_type="linear", asset="tokenized")
    rows = _read_daily_rows("scalp_sleeve")
    exp.freshness = _freshness(SCALP_STATE, rows, FRESH_MAX_AGE_H_RUNNER)
    st = _load_json_state(exp, SCALP_STATE)
    if st is None:
        return exp

    exp.equity_usd = float(st.get("equity", 0.0))
    realized_today = st.get("realized_today")
    exp.day_pnl_usd = _day_pnl_from_log(rows, fallback=realized_today)
    positions = st.get("positions") or {}
    gross = net = 0.0
    for p in positions.values():
        notional = float(p.get("qty", 0.0)) * float(p.get("entry", 0.0))
        gross += abs(notional)
        net += float(p.get("dir", 0)) * notional
    exp.gross_notional_usd = gross
    exp.net_notional_usd = net
    exp.position_open = bool(positions)
    exp.rails = _rails(SLEEVE_DAILY_LOSS_CAP_PCT, realized_today,
                       exp.equity_usd, bool(st.get("halted", False)))
    exp.detail = {
        "positions": {
            sym: {"dir": p.get("dir"), "entry": p.get("entry"),
                  "qty": p.get("qty"), "stop": p.get("stop"),
                  "notional_usd": round(abs(float(p.get("qty", 0.0))
                                            * float(p.get("entry", 0.0))), 2)}
            for sym, p in positions.items()},
        "n_trades": st.get("n_trades"),
        "wins": st.get("wins"),
    }
    return exp.finalize()


def _funding_carry_exposure() -> BookExposure:
    """Tokenized-stock funding carry (short rich perp vs long hedge).

    State: data/live_state/funding_carry_state.json (equity, pair with
    s_qty/s_entry/l_qty/l_entry legs or null, funding_collected).
    Gross = both leg notionals, net = long − short (≈0 by construction).
    No halt/daily-cap rails in the runner state → rails stay null.
    """
    exp = BookExposure(strategy_id="funding_carry", book_type="carry", asset="tokenized")
    rows = _read_daily_rows("funding_carry")
    exp.freshness = _freshness(CARRY_STATE, rows, FRESH_MAX_AGE_H_RUNNER)
    st = _load_json_state(exp, CARRY_STATE)
    if st is None:
        return exp

    exp.equity_usd = float(st.get("equity", 0.0))
    exp.day_pnl_usd = _day_pnl_from_log(rows)
    pair = st.get("pair")
    if pair:
        short_notional = float(pair.get("s_qty", 0.0)) * float(pair.get("s_entry", 0.0))
        long_notional = float(pair.get("l_qty", 0.0)) * float(pair.get("l_entry", 0.0))
        exp.gross_notional_usd = short_notional + long_notional
        exp.net_notional_usd = long_notional - short_notional
        exp.position_open = True
    exp.detail = {
        "pair": pair,
        "funding_collected_usd": float(st.get("funding_collected", 0.0)),
        "n_rounds": st.get("n_rounds"),
    }
    return exp.finalize()


def _mm_sleeve_exposure() -> BookExposure:
    """Tokenized-stock market-making sleeve (maker-only inventory book).

    State: data/live_state/mm_sleeve_state.json (cash, books{sym:{inv}},
    halted, realized_today). The daily jsonl's last row carries the
    marked inventory_usd map + total_equity — preferred for valuation;
    falls back to cash + inv×mid from state quotes if the log is empty.
    Gross = Σ|inv_usd|, net = Σ inv_usd.
    """
    exp = BookExposure(strategy_id="mm_sleeve", book_type="mm", asset="tokenized")
    rows = _read_daily_rows("mm_sleeve")
    exp.freshness = _freshness(MM_STATE, rows, FRESH_MAX_AGE_H_RUNNER)
    st = _load_json_state(exp, MM_STATE)
    if st is None:
        return exp

    books = st.get("books") or {}
    last = rows[-1] if rows else {}
    inv_usd: Dict[str, float] = {}
    if last.get("inventory_usd") is not None:
        inv_usd = {sym: float(v) for sym, v in (last.get("inventory_usd") or {}).items()}
        exp.equity_usd = float(last.get("total_equity", st.get("cash", 0.0)))
    else:  # no telemetry yet — mark inventory at state quote mids
        for sym, b in books.items():
            q = b.get("quotes") or {}
            mid = (float(q.get("bid", 0.0)) + float(q.get("ask", 0.0))) / 2.0
            inv_usd[sym] = float(b.get("inv", 0.0)) * mid
        exp.equity_usd = float(st.get("cash", 0.0)) + sum(inv_usd.values())

    exp.gross_notional_usd = sum(abs(v) for v in inv_usd.values())
    exp.net_notional_usd = sum(inv_usd.values())
    exp.position_open = any(abs(v) > 1e-9 for v in inv_usd.values())
    realized_today = st.get("realized_today")
    exp.day_pnl_usd = _day_pnl_from_log(rows, fallback=realized_today)
    exp.rails = _rails(SLEEVE_DAILY_LOSS_CAP_PCT, realized_today,
                       exp.equity_usd, bool(st.get("halted", False)))
    exp.detail = {
        "inventory": {
            sym: {"inv_shares": (books.get(sym) or {}).get("inv"),
                  "inv_usd": round(v, 2),
                  "fills": (books.get(sym) or {}).get("fills"),
                  "captured_usd": (books.get(sym) or {}).get("captured"),
                  "quotes": (books.get(sym) or {}).get("quotes")}
            for sym, v in inv_usd.items()},
        "cash_usd": float(st.get("cash", 0.0)),
    }
    return exp.finalize()


def collect_exposures() -> Dict[str, Any]:
    """Gather CURRENT positions from every monitored book (all six).

    Never raises: each book degrades independently to an 'error' entry.
    Aggregates only include cleanly-valued books.
    """
    providers = [
        ("spy_gamma_scalp", "options", "spy", _gamma_scalp_exposure),
        ("btc_wheel", "options", "btc",
         lambda: _wheel_exposure("btc_wheel", WHEEL_STATES["btc_wheel"])),
        ("btc_wheel_delta", "options", "btc",
         lambda: _wheel_exposure("btc_wheel_delta", WHEEL_STATES["btc_wheel_delta"])),
        ("scalp_sleeve", "linear", "tokenized", _scalp_sleeve_exposure),
        ("funding_carry", "carry", "tokenized", _funding_carry_exposure),
        ("mm_sleeve", "mm", "tokenized", _mm_sleeve_exposure),
    ]
    books: List[BookExposure] = []
    for sid, btype, asset, fn in providers:
        try:
            books.append(fn())
        except Exception as e:  # belt-and-braces; each fn already catches
            books.append(BookExposure(strategy_id=sid, book_type=btype,
                                      asset=asset, error=str(e)))

    ok = [b for b in books if b.error is None]
    combined_equity = sum(b.equity_usd for b in books if b.error is None or b.equity_usd)
    combined_day_pnl = sum(b.day_pnl_usd for b in ok)
    net_vega = sum(b.vega_usd for b in ok)
    notional = sum(b.short_vol_notional_usd for b in ok)
    combined_gross = sum(b.gross_notional_usd for b in ok)
    combined_net = sum(b.net_notional_usd for b in ok)
    return {
        "generated_at": _now_iso(),
        "strategies": {b.strategy_id: asdict(b) for b in books},
        "combined_equity_usd": round(combined_equity, 2),
        "combined_day_pnl_usd": round(combined_day_pnl, 2),
        "combined_gross_notional_usd": round(combined_gross, 2),
        "combined_net_notional_usd": round(combined_net, 2),
        "gross_leverage": round(combined_gross / combined_equity, 4)
        if combined_equity > 0 else 0.0,
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
    Linear/mm/carry books: first-order pnl = net_notional × spot_shock
    (no convexity in a perp book).
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
            if b.get("error") or b.get("asset") != domain:
                continue
            try:
                if b.get("book_type") in ("linear", "mm", "carry"):
                    pnl = float(b.get("net_notional_usd", 0.0)) \
                        * float(sc.get("spot_shock", 0.0))
                elif not b.get("position_open"):
                    continue
                elif domain == "btc":
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


# ── Equity curves ────────────────────────────────────────────────

def _equity_curves(days: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    """Per-book (date → total_equity) from the daily jsonl logs, last
    `days` distinct dates, plus a 'combined' series (per-date sum with
    forward-fill for books that didn't log that day). Never raises."""
    per_book: Dict[str, Dict[str, float]] = {}
    curves: Dict[str, List[Dict[str, Any]]] = {}
    for sid in BOOK_DAILY_LOGS:
        series: Dict[str, float] = {}
        for r in _read_daily_rows(sid):
            d, eq = _row_date(r), r.get("total_equity")
            if d and eq is not None:
                series[d] = float(eq)  # last row per date wins
        per_book[sid] = series
        dates = sorted(series)[-days:]
        curves[sid] = [{"date": d, "equity": round(series[d], 2)} for d in dates]

    all_dates = sorted({d for s in per_book.values() for d in s})
    combined: List[Dict[str, Any]] = []
    last_seen: Dict[str, float] = {}
    for d in all_dates:
        for sid, series in per_book.items():
            if d in series:
                last_seen[sid] = series[d]
        combined.append({"date": d, "equity": round(sum(last_seen.values()), 2),
                         "n_books": len(last_seen)})
    curves["combined"] = combined[-days:]
    return curves


# ── Report ───────────────────────────────────────────────────────

def portfolio_risk_report() -> Dict[str, Any]:
    """Exposures + budgets + utilization + breaches + advisory flags.

    Never raises. Budget checks:
      * abs_vega       — |net vega| vs max_abs_vega_usd
      * notional       — short-vol notional / combined equity vs max pct
      * gross_leverage — combined gross notional / equity vs max_gross_leverage
      * stress:<name>  — scenario loss_pct vs max_stress_loss_pct
    Breach attribution (advisory REDUCE_ONLY): the strategy contributing
    most to the breached metric.
    """
    try:
        exposures = collect_exposures()
    except Exception as e:  # truly unexpected — still return a report
        logger.exception("portfolio risk: exposure collection failed")
        return {"generated_at": _now_iso(), "error": str(e),
                "budgets": {}, "exposures": {}, "stress": {}, "equity_curves": {},
                "utilization": [], "breaches": [], "flags": {}, "advisory": True}

    try:
        cfg = load_budgets()
    except Exception as e:
        logger.exception("portfolio risk: budgets unreadable")
        return {"generated_at": exposures["generated_at"], "error": f"budgets unreadable: {e}",
                "budgets": {}, "exposures": exposures, "stress": {}, "equity_curves": {},
                "utilization": [], "breaches": [], "flags": {}, "advisory": True}

    budgets = cfg.get("budgets", {})
    scenarios = cfg.get("scenarios", {})
    stress_out = stress(exposures, scenarios)
    strategies = exposures.get("strategies", {})

    try:
        equity_curves = _equity_curves()
    except Exception as e:  # curves are cosmetic — never sink the report
        logger.warning("portfolio risk: equity curves failed: %s", e)
        equity_curves = {}

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

    def _offender(key: str, absolute: bool = False) -> Optional[str]:
        return max((sid for sid, b in strategies.items() if not b.get("error")),
                   key=lambda sid: abs(strategies[sid].get(key, 0.0)) if absolute
                   else strategies[sid].get(key, 0.0), default=None)

    # 1. Aggregate vega
    max_vega = float(budgets.get("max_abs_vega_usd", 0) or 0)
    _add("abs_vega", "Portfolio |vega|", exposures.get("abs_vega_usd", 0.0),
         max_vega, "$ / vol-pt", _offender("vega_usd", absolute=True))

    # 2. Short-vol notional
    max_pct = float(budgets.get("max_short_vol_notional_pct", 0) or 0)
    _add("short_vol_notional", "Short-vol notional / equity",
         exposures.get("short_vol_notional_pct", 0.0), max_pct, "x equity",
         _offender("short_vol_notional_usd"))

    # 3. Gross leverage across ALL books
    max_lev = float(budgets.get("max_gross_leverage", 0) or 0)
    _add("gross_leverage", "Gross leverage", exposures.get("gross_leverage", 0.0),
         max_lev, "x equity", _offender("gross_notional_usd"))

    # 4. Stress scenarios
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
        "equity_curves": equity_curves,
        "utilization": utilization,
        "breaches": breaches,
        "flags": flags,
        "degraded": exposures.get("degraded", []),
    }


if __name__ == "__main__":
    print(json.dumps(portfolio_risk_report(), indent=2, default=str))
