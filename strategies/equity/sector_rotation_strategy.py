"""Sector Rotation v1 — GMM regime + factor tilt + signal-tilted MV.

A StrategyBase subclass that integrates with the existing
``backtesting/engine/event_driven.py`` and live runner pipelines.

Approach
--------
1. Maintain a per-ticker ring buffer of bars.
2. On each new bar, update the buffer.
3. Every N bars (rebalance cadence, e.g. 10 = bi-weekly):
   a. Compute macro features from accumulated history
   b. Walk-forward refit GMM (or reuse last fit if recent enough)
   c. Predict current regime → label
   d. Compute cross-sectional factor z-scores
   e. Compose composite score using regime-conditional weights
   f. Run signal-tilted MV optimization on long sleeve
   g. Compute SPY beta hedge
   h. Emit TargetPosition list (positive longs, negative SPY)

Note: this strategy returns target ABSOLUTE QUANTITIES (not weights).
The runner layer translates target_quantity vs current_position to
trades. Position sizing uses the current equity tracked externally
(set_live_equity() in live mode; backtest engine handles via fills).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional

import numpy as np
import pandas as pd

from core.enums import SignalDirection
from core.logging import get_logger
from core.models import Bar, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy

from research.factors.equity import (
    compute_factor_panel,
    cross_sectional_zscore,
    composite_score_per_regime,
)
from research.portfolio import (
    ledoit_wolf_cov,
    optimize_long_sleeve,
    compute_spy_hedge,
)
from research.portfolio.cov_estimator import compute_returns_panel
from research.portfolio.mv_optimizer import compute_portfolio_beta
from research.regime import (
    GMMRegimeDetector,
    compute_macro_features,
    label_regimes,
)

logger = get_logger(__name__)


@register_strategy("sector_rotation_v1")
class SectorRotationStrategy(StrategyBase):
    """Multi-asset sector rotation with regime-conditional factor tilt."""

    # Rolling window of bars per ticker — must cover the longest factor
    # lookback (252 days for momentum 12-1). 800 days is comfortable.
    BAR_HISTORY_DAYS = 800

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Universe: comes from config["universe"] (list of tickers)
        self.universe: List[str] = self.params.get("universe", [])
        self.hedge_ticker: str = self.params.get("hedge_ticker", "SPY")

        # Required macro tickers in the panel for regime detection
        self.macro_tickers: List[str] = self.params.get(
            "macro_tickers",
            ["SPY", "^VIX", "^TNX", "^IRX", "HYG", "IEF"],
        )

        # All tickers we expect bars for
        self.all_symbols: List[str] = list(set(
            self.universe + [self.hedge_ticker] + self.macro_tickers
        ))

        # ── Rebalance cadence ──
        self.rebalance_cadence: int = int(self.params.get("rebalance_cadence_bars", 10))

        # ── Regime params ──
        self.regime_n_components = int(self.params.get("regime_n_components", 4))
        self.regime_train_window = int(self.params.get("regime_train_window_days", 756))
        self.regime_refit_every = int(self.params.get("regime_refit_every_days", 21))

        # ── Factor weights per regime (loaded from config) ──
        self.regime_factor_weights: Dict[str, Dict[str, float]] = (
            self.params.get("regime_factor_weights", {})
        )

        # ── MV optimization params ──
        self.cov_lookback = int(self.params.get("cov_lookback_days", 252))
        self.weight_min = float(self.params.get("weight_min", 0.0))
        self.weight_max = float(self.params.get("weight_max", 0.30))
        self.target_volatility = float(self.params.get("target_volatility", 0.08))
        self.tilt_strength = float(self.params.get("tilt_strength", 0.10))
        self.base_return_lookback = int(self.params.get("base_return_lookback_days", 60))

        # ── Hedge ──
        self.hedge_enabled = bool(self.params.get("hedge_enabled", True))
        self.max_short_pct = float(self.params.get("max_short_pct", 1.5))

        # ── State ──
        self._bars_history: Dict[str, Deque[Bar]] = {
            t: deque(maxlen=self.BAR_HISTORY_DAYS) for t in self.all_symbols
        }
        self._bars_seen: int = 0
        self._last_rebalance_bar: int = -1
        self._gmm: Optional[GMMRegimeDetector] = None
        self._gmm_last_fit_bar: int = -10_000
        self._regime_label_map: Dict[int, str] = {}
        self._current_target_weights: Dict[str, float] = {}
        self._live_equity: Optional[float] = None

        # Cache: most recent rebalance diagnostics
        self.last_diagnostic: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public hooks (used by backtest harness or live runner)
    # ------------------------------------------------------------------
    def set_live_equity(self, equity: float) -> None:
        """Update equity used for sizing target positions."""
        self._live_equity = equity

    def _get_equity(self) -> float:
        if self._live_equity is not None and self._live_equity > 0:
            return self._live_equity
        return float(self.params.get("initial_capital", 10000.0))

    # ------------------------------------------------------------------
    # Bar handler
    # ------------------------------------------------------------------
    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        if symbol in self._bars_history:
            self._bars_history[symbol].append(bar)
        # Count "calendar bars seen" by the universe member (any ticker)
        # — using a representative anchor: SPY if available, else first universe member
        anchor = "SPY" if "SPY" in self._bars_history else (
            self.universe[0] if self.universe else None
        )
        if anchor and symbol == anchor:
            self._bars_seen += 1

    # ------------------------------------------------------------------
    # Build long-format panel from bar history
    # ------------------------------------------------------------------
    def _build_panel(self) -> pd.DataFrame:
        rows = []
        for ticker, dq in self._bars_history.items():
            for b in dq:
                rows.append({
                    "timestamp": b.timestamp,
                    "ticker": ticker,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.sort_values(["ticker", "timestamp"]).reset_index(drop=True)

    # ------------------------------------------------------------------
    # Rebalance
    # ------------------------------------------------------------------
    def _should_rebalance(self) -> bool:
        if self._bars_seen < 260:  # need ~1y of history before any signal
            return False
        if self._bars_seen - self._last_rebalance_bar < self.rebalance_cadence:
            return False
        return True

    def _rebalance(self) -> Dict[str, float]:
        """Run full pipeline → return target weight dict (incl. SPY hedge)."""
        panel = self._build_panel()
        if panel.empty:
            return {}

        # ── 1. Macro features + GMM regime ──
        feats = compute_macro_features(panel)
        feats_clean = feats.dropna()

        regime_label = "neutral"  # default
        if len(feats_clean) >= self.regime_train_window // 2:
            try:
                # Refit GMM if stale
                if (
                    self._gmm is None
                    or (self._bars_seen - self._gmm_last_fit_bar) >= self.regime_refit_every
                ):
                    train_window = feats_clean.iloc[-self.regime_train_window:]
                    self._gmm = GMMRegimeDetector(
                        n_components=self.regime_n_components
                    ).fit(train_window)
                    self._gmm_last_fit_bar = self._bars_seen
                    centroids = self._gmm.cluster_centroids()
                    label_map = label_regimes(centroids, n_components=self.regime_n_components)
                    self._regime_label_map = {int(k): v.value for k, v in label_map.items()}

                # Predict current regime
                latest_feat = feats_clean.iloc[-1]
                cluster_id = self._gmm.predict_one(latest_feat)
                regime_label = self._regime_label_map.get(cluster_id, "neutral")
            except Exception as e:
                logger.warning("regime_detection_failed", error=str(e))
                regime_label = "neutral"

        # ── 2. Cross-sectional factor scores for the universe ──
        try:
            factor_panel = compute_factor_panel(panel, self.universe)
            if factor_panel.empty:
                return {}
            z_panel = cross_sectional_zscore(factor_panel)
        except Exception as e:
            logger.warning("factor_compute_failed", error=str(e))
            return {}

        # Take only the latest date's z-scores
        latest_ts = z_panel["timestamp"].max()
        z_today = z_panel[z_panel["timestamp"] == latest_ts]

        # ── 3. Composite score using regime weights ──
        weights = self.regime_factor_weights.get(regime_label, {})
        if not weights:
            return {}
        scores: Dict[str, float] = {}
        for _, row in z_today.iterrows():
            t = row["ticker"]
            s = 0.0
            for fname, w in weights.items():
                v = row.get(fname)
                if v is not None and not pd.isna(v):
                    s += w * float(v)
            scores[t] = s

        # ── 4. Build expected returns: μ = base_60d_return × (1 + tilt × score) ──
        wide_close = panel.pivot_table(
            index="timestamp", columns="ticker", values="close", aggfunc="last"
        ).sort_index()
        # Drop tickers with insufficient history
        valid_tickers = []
        for t in self.universe:
            if t in wide_close.columns and wide_close[t].dropna().shape[0] >= self.cov_lookback:
                valid_tickers.append(t)

        if len(valid_tickers) < 3:
            return {}

        wide_close = wide_close[valid_tickers]
        log_ret = np.log(wide_close / wide_close.shift(1))

        base_returns = (
            wide_close.iloc[-1] / wide_close.iloc[-self.base_return_lookback] - 1
        ) * (252.0 / self.base_return_lookback)
        base_returns = base_returns.dropna()

        score_series = pd.Series(scores).reindex(base_returns.index).fillna(0.0)
        expected_returns = base_returns * (1 + self.tilt_strength * score_series)

        # ── 5. Cov matrix (Ledoit-Wolf) ──
        recent_returns = log_ret.iloc[-self.cov_lookback:][base_returns.index].dropna(how="all")
        cov_matrix = ledoit_wolf_cov(recent_returns, annualization_factor=252)
        if cov_matrix.empty:
            return {}

        # ── 6. MV optimize long sleeve ──
        long_weights = optimize_long_sleeve(
            expected_returns,
            cov_matrix,
            weight_min=self.weight_min,
            weight_max=self.weight_max,
            target_volatility=self.target_volatility,
        )
        if not long_weights:
            return {}

        # ── 7. SPY hedge ──
        final_weights: Dict[str, float] = dict(long_weights)
        if self.hedge_enabled and self.hedge_ticker in self._bars_history:
            # Use the (sign-flipped) beta_spy z-score as a proxy for actual beta
            # Or just compute betas from log_ret vs SPY log_ret
            if "SPY" in wide_close.columns:
                spy_ret = np.log(wide_close.get("SPY", pd.Series()) /
                                 wide_close.get("SPY", pd.Series()).shift(1))
            else:
                # SPY not in wide_close; use macro
                spy_panel = panel[panel["ticker"] == "SPY"].set_index("timestamp")["close"]
                spy_ret = np.log(spy_panel / spy_panel.shift(1))

            # Rolling 252d betas vs SPY
            betas = pd.Series(dtype="float64")
            for t in long_weights:
                if t in log_ret.columns:
                    r = log_ret[t].iloc[-self.cov_lookback:]
                    s = spy_ret.reindex(r.index)
                    pair = pd.concat([r, s], axis=1).dropna()
                    if len(pair) >= self.cov_lookback // 2:
                        cov_ts = pair.iloc[:, 0].cov(pair.iloc[:, 1])
                        var_s = pair.iloc[:, 1].var()
                        if var_s > 0:
                            betas[t] = cov_ts / var_s
                        else:
                            betas[t] = 1.0
                    else:
                        betas[t] = 1.0
                else:
                    betas[t] = 1.0

            port_beta = compute_portfolio_beta(long_weights, betas)
            spy_short_dollars = compute_spy_hedge(
                long_weights, 1.0, port_beta, max_short_pct=self.max_short_pct,
            )
            if spy_short_dollars > 0:
                final_weights[self.hedge_ticker] = -spy_short_dollars

        # Save diagnostic
        self.last_diagnostic = {
            "rebalance_bar": self._bars_seen,
            "rebalance_ts": str(latest_ts),
            "regime": regime_label,
            "n_long_positions": len(long_weights),
            "long_gross": sum(long_weights.values()),
            "spy_hedge_dollars": final_weights.get(self.hedge_ticker, 0.0),
            "scores": {k: round(v, 3) for k, v in scores.items()},
        }
        self._current_target_weights = final_weights
        self._last_rebalance_bar = self._bars_seen
        return final_weights

    # ------------------------------------------------------------------
    # Generate target positions (called by event-driven backtest each bar)
    # ------------------------------------------------------------------
    def calculate_target_positions(self) -> List[TargetPosition]:
        if not self._should_rebalance():
            return []

        weights = self._rebalance()
        if not weights:
            return []

        equity = self._get_equity()
        targets: List[TargetPosition] = []
        for ticker, weight in weights.items():
            # Look up latest price from history
            dq = self._bars_history.get(ticker)
            if not dq or len(dq) == 0:
                continue
            last_close = dq[-1].close
            if last_close <= 0:
                continue
            # weight × equity = $ allocation; / price = shares
            dollars = weight * equity
            qty = dollars / last_close
            if abs(qty) < 1e-6:
                continue
            targets.append(TargetPosition(
                symbol=ticker,
                strategy_id=self.strategy_id,
                target_quantity=qty,
                target_weight=weight,
                urgency=0.5,
                timestamp=dq[-1].timestamp,
            ))

        logger.info(
            "rebalance_targets_emitted",
            n=len(targets),
            regime=self.last_diagnostic.get("regime"),
            bar=self._bars_seen,
        )
        return targets

    def generate_signals(self) -> List[Signal]:
        # We don't use Signal-level logic; targets come straight from MV
        return []
