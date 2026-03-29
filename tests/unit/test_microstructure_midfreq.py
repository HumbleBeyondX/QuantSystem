"""Tests for mid-frequency microstructure factors and cost model.

Tests cover:
    1. Factor computation functions with known inputs/outputs
    2. Cost model profitability gating
    3. Position sizing with depth constraints
    4. Composite score computation
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np
import pytest

from data.features.microstructure_midfreq import (
    compute_composite_score,
    compute_microprice_deviation,
    compute_obi_midfreq,
    compute_spread_zscore,
    compute_tfi_midfreq,
    compute_vpin_bars,
    compute_whale_signal,
)
from execution.models.cost_model import CostModel


# ============================================================
# Factor Tests
# ============================================================


class TestOBI:
    """Order Book Imbalance tests."""

    def test_empty_snapshots(self):
        assert compute_obi_midfreq([]) == 0.0

    def test_all_bids_no_asks(self):
        """100% buy pressure → OBI near +1."""
        snaps = [{"bids": [(100, 10)], "asks": [(101, 0)]} for _ in range(5)]
        obi = compute_obi_midfreq(snaps, levels=1)
        assert obi > 0.9

    def test_all_asks_no_bids(self):
        """100% sell pressure → OBI near -1."""
        snaps = [{"bids": [(100, 0)], "asks": [(101, 10)]} for _ in range(5)]
        obi = compute_obi_midfreq(snaps, levels=1)
        assert obi < -0.9

    def test_balanced_book(self):
        """Equal depth → OBI near 0."""
        snaps = [{"bids": [(100, 10)], "asks": [(101, 10)]} for _ in range(10)]
        obi = compute_obi_midfreq(snaps, levels=1)
        assert abs(obi) < 0.1

    def test_ema_weighting(self):
        """Later snapshots should have more influence with EMA."""
        # Early snapshots: balanced, late snapshots: heavy bids
        snaps = [{"bids": [(100, 5)], "asks": [(101, 5)]} for _ in range(10)]
        snaps += [{"bids": [(100, 20)], "asks": [(101, 1)]} for _ in range(5)]
        obi = compute_obi_midfreq(snaps, levels=1, decay=0.90)
        assert obi > 0.3  # Should lean positive due to recent bid dominance

    def test_multi_level_depth(self):
        """Multi-level aggregation should sum across levels."""
        bids = [(100, 5), (99, 10), (98, 15)]
        asks = [(101, 3), (102, 3), (103, 3)]
        snaps = [{"bids": bids, "asks": asks} for _ in range(5)]
        obi = compute_obi_midfreq(snaps, levels=3)
        # Bid depth: 30, Ask depth: 9, imbalance = 21/39 ≈ 0.538
        assert obi > 0.4


class TestTFI:
    """Trade Flow Imbalance tests."""

    def test_empty_trades(self):
        assert compute_tfi_midfreq([]) == 0.0

    def test_all_buys(self):
        trades = [{"side": "buy", "amount": 1.0} for _ in range(10)]
        assert compute_tfi_midfreq(trades) == 1.0

    def test_all_sells(self):
        trades = [{"side": "sell", "amount": 1.0} for _ in range(10)]
        assert compute_tfi_midfreq(trades) == -1.0

    def test_balanced_flow(self):
        trades = [
            {"side": "buy", "amount": 5.0},
            {"side": "sell", "amount": 5.0},
        ]
        assert abs(compute_tfi_midfreq(trades)) < 0.01

    def test_volume_weighted(self):
        """Large buy should dominate over many small sells."""
        trades = [
            {"side": "buy", "amount": 100.0},
            {"side": "sell", "amount": 1.0},
            {"side": "sell", "amount": 1.0},
        ]
        tfi = compute_tfi_midfreq(trades)
        assert tfi > 0.9


class TestSpreadZScore:
    """Spread Z-Score tests."""

    def test_insufficient_history(self):
        history = deque([1.0, 2.0])
        assert compute_spread_zscore(1.5, history) == 0.0

    def test_normal_spread(self):
        """Spread at mean → z-score near 0."""
        history = deque([5.0] * 50)
        z = compute_spread_zscore(5.0, history)
        assert abs(z) < 0.01

    def test_tight_spread(self):
        """Spread below mean → negative z-score (favorable)."""
        # Need variance in history for std > 0
        history = deque([10.0 + np.random.normal(0, 1) for _ in range(50)])
        z = compute_spread_zscore(5.0, history, lookback=50)
        assert z < -1.0

    def test_wide_spread(self):
        """Spread above mean → positive z-score (unfavorable)."""
        history = deque([5.0 + np.random.normal(0, 0.5) for _ in range(50)])
        z = compute_spread_zscore(15.0, history, lookback=50)
        assert z > 1.0

    def test_clamped_to_3(self):
        """Z-score should be clamped to [-3, 3]."""
        history = deque([5.0 + np.random.normal(0, 0.5) for _ in range(50)])
        z = compute_spread_zscore(500.0, history, lookback=50)
        assert z == 3.0


class TestMicropriceDeviation:
    """Microprice Deviation tests."""

    def test_empty_snapshots(self):
        assert compute_microprice_deviation([]) == 0.0

    def test_balanced_book(self):
        """Equal quantities → microprice = mid → deviation ≈ 0."""
        snaps = [{"best_bid": 100, "best_ask": 101, "bid_qty": 10, "ask_qty": 10}]
        dev = compute_microprice_deviation(snaps)
        assert abs(dev) < 0.1

    def test_buy_pressure(self):
        """More ask qty → microprice shifts toward bid → but that's wrong.
        Actually: microprice = (bid * ask_qty + ask * bid_qty) / total
        More ask_qty → more weight on bid → microprice < mid? No.
        bid=100, ask=101, bid_qty=1, ask_qty=100:
        microprice = (100*100 + 101*1) / 101 = 10101/101 ≈ 100.01
        mid = 100.5
        deviation = (100.01 - 100.5) / 100.5 * 10000 ≈ -48.8 bps (negative = sell pressure)

        More bid_qty → microprice = (100*1 + 101*100) / 101 ≈ 100.99 > mid → positive
        """
        snaps = [{"best_bid": 100, "best_ask": 101, "bid_qty": 100, "ask_qty": 1}]
        dev = compute_microprice_deviation(snaps)
        assert dev > 0  # Microprice above mid = buy pressure


class TestWhaleSignal:
    """Whale Signal tests."""

    def test_no_whales(self):
        trades = [{"side": "buy", "amount": 0.1}]
        result = compute_whale_signal(trades, median_amount=0.1, multiplier=10.0)
        assert result["whale_imbalance"] == 0.0

    def test_whale_buy(self):
        trades = [{"side": "buy", "amount": 100.0}]
        result = compute_whale_signal(trades, median_amount=1.0, multiplier=10.0)
        assert result["whale_buy_vol"] == 100.0
        assert result["whale_imbalance"] == 1.0

    def test_whale_sell(self):
        trades = [{"side": "sell", "amount": 50.0}]
        result = compute_whale_signal(trades, median_amount=1.0, multiplier=10.0)
        assert result["whale_sell_vol"] == 50.0
        assert result["whale_imbalance"] == -1.0


class TestVPIN:
    """VPIN tests."""

    def test_insufficient_data(self):
        bars = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}]
        assert compute_vpin_bars(bars, n_buckets=50) == 0.0

    def test_strong_trend(self):
        """Consistent up bars → high buy classification → moderate VPIN."""
        bars = [
            {"open": 100 + i, "high": 102 + i, "low": 99 + i,
             "close": 101 + i, "volume": 100}
            for i in range(60)
        ]
        vpin = compute_vpin_bars(bars, n_buckets=50)
        assert 0.0 < vpin <= 1.0

    def test_doji_bars(self):
        """Open == Close bars → 50/50 split → low VPIN."""
        bars = [
            {"open": 100, "high": 102, "low": 98, "close": 100, "volume": 100}
            for _ in range(60)
        ]
        vpin = compute_vpin_bars(bars, n_buckets=50)
        assert vpin < 0.1


class TestComposite:
    """Composite score tests."""

    def test_all_zero(self):
        score = compute_composite_score(0, 0, 0, 0, 0, 0.5)
        assert abs(score) < 0.1

    def test_strong_buy(self):
        """All factors positive → strong positive composite."""
        score = compute_composite_score(
            obi=0.8, tfi=0.7, spread_zscore=-2.0,
            microprice_dev=5.0, whale_imbalance=0.6, vpin=0.1,
        )
        assert score > 0.5

    def test_strong_sell(self):
        """All factors negative → strong negative composite."""
        score = compute_composite_score(
            obi=-0.8, tfi=-0.7, spread_zscore=2.0,
            microprice_dev=-5.0, whale_imbalance=-0.6, vpin=0.9,
        )
        assert score < -0.3

    def test_bounded(self):
        """Output should always be in [-1, 1]."""
        for _ in range(100):
            score = compute_composite_score(
                obi=np.random.uniform(-1, 1),
                tfi=np.random.uniform(-1, 1),
                spread_zscore=np.random.uniform(-3, 3),
                microprice_dev=np.random.uniform(-10, 10),
                whale_imbalance=np.random.uniform(-1, 1),
                vpin=np.random.uniform(0, 1),
            )
            assert -1 <= score <= 1


# ============================================================
# Cost Model Tests
# ============================================================


class TestCostModel:
    """Cost model tests."""

    @pytest.fixture
    def model(self):
        return CostModel(
            maker_fee=0.0002,
            taker_fee=0.0004,
            base_slippage_bps=1.0,
            depth_impact_factor=0.05,
            min_profit_to_cost_ratio=2.0,
        )

    def test_round_trip_cost_basic(self, model):
        """Basic round-trip should include 2x taker fee + 2x slippage."""
        cost = model.estimate_round_trip_cost_bps(
            price=50000, quantity=0.01, orderbook_depth=10.0
        )
        # 2x taker = 2 * 4 = 8 bps fee
        # Slippage depends on depth
        assert cost > 8  # Must be at least the fee

    def test_profitability_gate_rejects_low_signal(self, model):
        """Small expected move should be rejected."""
        assert not model.is_trade_profitable(
            expected_move_bps=5, quantity=0.1, price=50000, orderbook_depth=10.0
        )

    def test_profitability_gate_accepts_high_signal(self, model):
        """Large expected move should pass."""
        assert model.is_trade_profitable(
            expected_move_bps=50, quantity=0.01, price=50000, orderbook_depth=100.0
        )

    def test_slippage_increases_with_size(self, model):
        """Larger orders should have more slippage."""
        small = model.estimate_slippage_bps(0.01, 50000, 10.0)
        large = model.estimate_slippage_bps(1.0, 50000, 10.0)
        assert large > small

    def test_optimal_quantity_zero_for_no_signal(self, model):
        """Zero signal → zero quantity."""
        qty = model.optimal_quantity(0.0, 5000, 50000, 10.0)
        assert qty == 0.0

    def test_optimal_quantity_scales_with_strength(self, model):
        """Stronger signal → larger position (with deep enough book)."""
        # Use very deep book so depth cap doesn't interfere
        weak = model.optimal_quantity(0.3, 5000, 50000, 10000.0)
        strong = model.optimal_quantity(0.8, 5000, 50000, 10000.0)
        assert strong > weak

    def test_optimal_quantity_capped_by_depth(self, model):
        """Shallow book should cap position size."""
        deep = model.optimal_quantity(1.0, 50000, 50000, 1000.0)
        shallow = model.optimal_quantity(1.0, 50000, 50000, 0.1)
        assert deep > shallow

    def test_cost_breakdown_structure(self, model):
        """CostBreakdown should have all required fields."""
        bd = model.estimate_round_trip_cost(50000, 0.1, 10.0)
        assert bd.fee_bps > 0
        assert bd.slippage_bps > 0
        assert bd.total_bps == bd.fee_bps + bd.slippage_bps
        assert bd.total_usd > 0
        assert abs(bd.total_usd - (bd.fee_usd + bd.slippage_usd)) < 0.01
