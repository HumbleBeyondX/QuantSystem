"""Gamma squeeze daily scanner.

For each ticker in a universe:
  1. Pull current option chain (yfinance) + spot + 30d price/vol history
  2. Compute six setup signals:
       - GEX proxy (estimated dealer net gamma exposure)
       - Call/Put OI imbalance
       - OTM call concentration (5-15% OTM share of total call OI)
       - Volume surge (today vs 30d avg)
       - Short interest % of float (from yfinance.info)
       - IV rank (low IV = room to expand)
       - Spot proximity to max-OI strike (catalyst proximity)
  3. Cross-sectionally z-score each signal
  4. Composite score = weighted sum per universe.yaml
  5. Return ranked candidates with all signal components

Pure computation — no I/O beyond yfinance. CLI wrapper is in
scripts/scan_gamma_squeeze.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yaml
from scipy.stats import norm

from core.logging import get_logger

logger = get_logger(__name__)


DEFAULT_UNIVERSE_PATH = (
    Path(__file__).resolve().parents[2]
    / "config" / "gamma_squeeze" / "universe.yaml"
)


# ────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────

@dataclass
class SqueezeCandidate:
    ticker: str
    category: str
    spot: float
    last_volume: float
    avg_volume_30d: float

    # Raw signal components
    gex_proxy: float
    cp_oi_ratio: float
    otm_call_concentration: float
    volume_surge: float
    short_interest_pct: float
    iv_rank: float
    spot_to_max_oi_pct: float

    # Z-scored signal components (filled later, cross-sectionally)
    z_gex_proxy: float = 0.0
    z_cp_oi_ratio: float = 0.0
    z_otm_call_concentration: float = 0.0
    z_volume_surge: float = 0.0
    z_short_interest: float = 0.0
    z_iv_rank: float = 0.0
    z_spot_to_max_oi: float = 0.0

    composite_score: float = 0.0

    # Diagnostic / actionable details
    max_oi_strike: float = 0.0
    nearest_expiry: str = ""
    total_call_oi: int = 0
    total_put_oi: int = 0
    n_strikes: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ────────────────────────────────────────────────────────────
# Loaders
# ────────────────────────────────────────────────────────────

def load_universe(path: Path = DEFAULT_UNIVERSE_PATH) -> Dict[str, Any]:
    """Load the universe config. Returns the full dict (tickers + filters + weights)."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def flatten_tickers(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    """Flatten the nested category dict into [{ticker, category}, ...] entries.

    A ticker appearing in multiple categories keeps its FIRST category label
    (kept stable for downstream sorting / grouping).
    """
    seen: Dict[str, str] = {}
    for cat_name, cat_body in (cfg.get("categories") or {}).items():
        for t in cat_body.get("tickers", []):
            if t not in seen:
                seen[t] = cat_name
    return [{"ticker": t, "category": c} for t, c in seen.items()]


# ────────────────────────────────────────────────────────────
# Black-Scholes gamma
# ────────────────────────────────────────────────────────────

def bs_gamma(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    r: float = 0.045,
) -> float:
    """Standard BS gamma. Returns dollar-gamma per 1-share-equiv contract.

    Returns 0 for degenerate inputs (negative T, missing IV).
    """
    if t_years <= 0 or iv <= 0 or strike <= 0 or spot <= 0:
        return 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    return norm.pdf(d1) / (spot * iv * sqrt_t)


# ────────────────────────────────────────────────────────────
# Scanner
# ────────────────────────────────────────────────────────────

class GammaSqueezeScanner:
    """Stateless scanner — one call per universe scan."""

    def __init__(
        self,
        max_dte: int = 60,
        min_dollar_volume: float = 5_000_000,
        min_option_strikes: int = 20,
        risk_free_rate: float = 0.045,
        otm_band: tuple = (0.05, 0.15),
        iv_lookback_days: int = 252,
        data_source: str = "alpaca",
    ):
        self.max_dte = max_dte
        self.min_dollar_volume = min_dollar_volume
        self.min_option_strikes = min_option_strikes
        self.risk_free_rate = risk_free_rate
        # Chain source: Alpaca OPRA (real greeks/IV/OI) preferred; yfinance
        # fallback keeps the scanner usable without credentials.
        self._alpaca = None
        if data_source == "alpaca":
            try:
                from research.gamma_squeeze.alpaca_data import AlpacaOptionsProvider
                self._alpaca = AlpacaOptionsProvider()
            except Exception as e:
                import warnings
                warnings.warn(f"Alpaca chain provider unavailable ({e}); "
                              f"falling back to yfinance chains + BS greeks")
        self.otm_band = otm_band
        self.iv_lookback_days = iv_lookback_days

    # ── per-ticker computation ──
    def scan_one(self, ticker: str, category: str = "uncategorized") -> SqueezeCandidate:
        import yfinance as yf

        result = SqueezeCandidate(
            ticker=ticker, category=category, spot=0.0,
            last_volume=0.0, avg_volume_30d=0.0,
            gex_proxy=0.0, cp_oi_ratio=0.0, otm_call_concentration=0.0,
            volume_surge=0.0, short_interest_pct=0.0, iv_rank=0.0,
            spot_to_max_oi_pct=0.0,
        )

        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=f"{self.iv_lookback_days}d", interval="1d", auto_adjust=False)
            if hist.empty or len(hist) < 30:
                result.error = "insufficient_history"
                return result

            spot = float(hist["Close"].iloc[-1])
            last_vol = float(hist["Volume"].iloc[-1])
            avg_vol_30d = float(hist["Volume"].iloc[-30:].mean())

            result.spot = spot
            result.last_volume = last_vol
            result.avg_volume_30d = avg_vol_30d

            # Dollar volume liquidity gate
            if spot * avg_vol_30d < self.min_dollar_volume:
                result.error = "below_liquidity_threshold"
                return result

            # ── Option chain: Alpaca OPRA (real greeks) or yfinance fallback ──
            if self._alpaca is not None:
                try:
                    chain_df = self._alpaca.get_chain(ticker, self.max_dte)
                except Exception as e:
                    result.error = f"alpaca_chain_failed:{e}"
                    return result
                if chain_df.empty:
                    result.error = "no_chain_data"
                    return result
                # Anomaly guard: a chain that parses but is all zeros poisoned
                # this scanner for two weeks (yfinance incident, 2026-07).
                from data.quality.guards import chain_sanity
                rep = chain_sanity(chain_df, ticker)
                if not rep.ok:
                    result.error = f"chain_anomaly:{';'.join(rep.issues)}"
                    return result
                calls_df = chain_df[chain_df["type"] == "call"].copy()
                puts_df = chain_df[chain_df["type"] == "put"].copy()
                if calls_df.empty:
                    result.error = "no_chain_data"
                    return result
                result.nearest_expiry = str(calls_df["expiry"].min())
            else:
                try:
                    expirations = t.options or []
                except Exception as e:
                    result.error = f"options_unavailable:{e}"
                    return result

                today = date.today()
                usable_exps = []
                for exp_str in expirations:
                    try:
                        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                        dte = (exp_date - today).days
                        if 1 <= dte <= self.max_dte:
                            usable_exps.append((exp_str, dte))
                    except ValueError:
                        continue

                if not usable_exps:
                    result.error = "no_usable_expirations"
                    return result

                all_calls: List[pd.DataFrame] = []
                all_puts: List[pd.DataFrame] = []
                for exp_str, dte in usable_exps[:6]:  # cap at 6 expiries for speed
                    try:
                        chain = t.option_chain(exp_str)
                        calls = chain.calls.copy()
                        puts = chain.puts.copy()
                        calls["dte"] = dte
                        puts["dte"] = dte
                        all_calls.append(calls)
                        all_puts.append(puts)
                    except Exception:
                        continue

                if not all_calls:
                    result.error = "no_chain_data"
                    return result

                calls_df = pd.concat(all_calls, ignore_index=True)
                puts_df = pd.concat(all_puts, ignore_index=True)
                result.nearest_expiry = str(usable_exps[0][0])

            n_strikes = calls_df["strike"].nunique()
            result.n_strikes = int(n_strikes)

            if n_strikes < self.min_option_strikes:
                result.error = "thin_chain"
                return result

            # Normalize OI to non-NaN
            calls_df["openInterest"] = calls_df["openInterest"].fillna(0).astype(float)
            puts_df["openInterest"] = puts_df["openInterest"].fillna(0).astype(float)
            calls_df["impliedVolatility"] = calls_df["impliedVolatility"].fillna(0).astype(float)

            total_call_oi = float(calls_df["openInterest"].sum())
            total_put_oi = float(puts_df["openInterest"].sum())
            result.total_call_oi = int(total_call_oi)
            result.total_put_oi = int(total_put_oi)

            # ── Signal 1: GEX proxy (estimated dealer net gamma) ──
            # Assumption: dealers are short calls, long puts → net gamma sign
            # comes from imbalance. Approximate dollar gamma per option as:
            #   contract_gamma × OI × 100 × spot²  (in $ per 1% move)
            # Prefer exchange-computed gamma (Alpaca OPRA snapshots); fall
            # back to Black-Scholes only for contracts missing greeks.
            def _row_gamma(row) -> float:
                g = float(row.get("gamma", 0) or 0)
                if g > 0:
                    return g
                iv = float(row.get("impliedVolatility", 0) or 0)
                if iv <= 0:
                    return 0.0
                return bs_gamma(spot, float(row["strike"]),
                                row["dte"] / 365, iv, self.risk_free_rate)

            call_gex_dollars = 0.0
            for _, row in calls_df.iterrows():
                oi = float(row["openInterest"])
                if oi <= 0:
                    continue
                gamma = _row_gamma(row)
                call_gex_dollars += gamma * oi * 100 * (spot ** 2) * 0.01

            put_gex_dollars = 0.0
            puts_df["impliedVolatility"] = puts_df["impliedVolatility"].fillna(0).astype(float)
            for _, row in puts_df.iterrows():
                oi = float(row["openInterest"])
                if oi <= 0:
                    continue
                gamma = _row_gamma(row)
                put_gex_dollars += gamma * oi * 100 * (spot ** 2) * 0.01

            # Dealer net gamma ≈ −(call GEX) + put GEX
            # Negative dealer gamma = bullish squeeze setup. We return the
            # NEGATED value so higher score = more squeeze potential.
            result.gex_proxy = float(call_gex_dollars - put_gex_dollars) / 1e6  # M$

            # ── Signal 2: Call/Put OI ratio ──
            result.cp_oi_ratio = total_call_oi / max(total_put_oi, 1)

            # ── Signal 3: OTM call concentration ──
            band_lo, band_hi = self.otm_band
            otm_mask = (
                (calls_df["strike"] > spot * (1 + band_lo))
                & (calls_df["strike"] < spot * (1 + band_hi))
            )
            otm_call_oi = float(calls_df.loc[otm_mask, "openInterest"].sum())
            result.otm_call_concentration = otm_call_oi / max(total_call_oi, 1)

            # ── Signal 4: Volume surge ──
            result.volume_surge = last_vol / max(avg_vol_30d, 1)

            # ── Signal 5: Short interest % (yfinance.info, best-effort) ──
            try:
                info = t.info
                si = info.get("shortPercentOfFloat", None)
                if si is not None:
                    result.short_interest_pct = float(si) * 100  # convert to %
            except Exception:
                pass

            # ── Signal 6: IV rank (low IV = expansion potential) ──
            # Approximate via realized vol percentile of last `iv_lookback_days`
            log_ret = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
            if len(log_ret) >= 30:
                rv_21d = log_ret.rolling(21).std() * np.sqrt(252)
                current_rv = rv_21d.iloc[-1]
                # Percentile within history
                iv_rank = (rv_21d < current_rv).mean()
                # LOW iv = high score → invert
                result.iv_rank = 1.0 - float(iv_rank)

            # ── Signal 7: Spot proximity to max-OI strike ──
            # Find strike with maximum aggregate call OI (the "magnet" strike).
            # If spot is BELOW it and close (within 5%), it's a high-conviction
            # setup (squeeze drives to the magnet).
            max_oi_idx = calls_df["openInterest"].idxmax()
            if pd.notna(max_oi_idx):
                max_oi_strike = float(calls_df.loc[max_oi_idx, "strike"])
                result.max_oi_strike = max_oi_strike
                if max_oi_strike > spot:
                    dist_pct = (max_oi_strike - spot) / spot
                    # Score = higher when closer, in range (0, 0.10)
                    result.spot_to_max_oi_pct = max(0, 0.10 - dist_pct) / 0.10
                else:
                    # Spot already above magnet → setup expired
                    result.spot_to_max_oi_pct = 0.0

            return result

        except Exception as e:
            result.error = f"scan_exception:{type(e).__name__}:{e}"
            return result

    # ── universe-level (z-score + composite) ──
    def scan_universe(
        self,
        config: Dict[str, Any],
        verbose: bool = False,
    ) -> pd.DataFrame:
        tickers = flatten_tickers(config)
        weights = config.get("scoring", {}).get("weights", {})

        results: List[SqueezeCandidate] = []
        for i, t in enumerate(tickers, 1):
            if verbose:
                logger.info("scanning_ticker", n=i, total=len(tickers), ticker=t["ticker"])
            c = self.scan_one(t["ticker"], t["category"])
            results.append(c)

        df = pd.DataFrame([r.to_dict() for r in results])

        # Keep only those without errors AND with at least one signal
        ok = df[df["error"].isnull() | (df["error"] == "")].copy()

        # Cross-sectional z-scoring per signal
        signal_to_field = {
            "gex_proxy": "z_gex_proxy",
            "cp_oi_ratio": "z_cp_oi_ratio",
            "otm_call_concentration": "z_otm_call_concentration",
            "volume_surge": "z_volume_surge",
            "short_interest": "z_short_interest",
            "iv_rank": "z_iv_rank",
            "spot_near_max_oi": "z_spot_to_max_oi",
        }
        raw_for_z = {
            "gex_proxy": "gex_proxy",
            "cp_oi_ratio": "cp_oi_ratio",
            "otm_call_concentration": "otm_call_concentration",
            "volume_surge": "volume_surge",
            "short_interest": "short_interest_pct",
            "iv_rank": "iv_rank",
            "spot_near_max_oi": "spot_to_max_oi_pct",
        }

        for sig, z_field in signal_to_field.items():
            raw_field = raw_for_z[sig]
            if raw_field not in ok.columns:
                continue
            s = ok[raw_field].astype(float)
            mu = s.mean()
            sd = s.std(ddof=0)
            ok[z_field] = (s - mu) / sd if sd > 0 else 0.0
            ok[z_field] = ok[z_field].clip(-3, 3)  # cap outliers

        # Composite score
        ok["composite_score"] = 0.0
        for sig, z_field in signal_to_field.items():
            w = float(weights.get(sig, 0))
            if z_field in ok.columns:
                ok["composite_score"] += w * ok[z_field]

        # Merge errors back so caller can see failed tickers
        bad = df[~(df["error"].isnull() | (df["error"] == ""))]
        out = pd.concat([ok, bad], ignore_index=True)
        out = out.sort_values("composite_score", ascending=False, na_position="last")
        return out.reset_index(drop=True)
