"""Unit tests: TakerShadow / MakerShadow fill engines + RiskRails.

Table-driven coverage of the branches the backtest smoke can't isolate:
netting, flip-through-zero, quote TTL, stop/trail/time exits, sizing,
liquidation clamp, margin cap — plus regressions for the maker-metrics
and bar-cache fixes.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from core.runtime.fills import MakerShadow, TakerShadow
from core.runtime.intents import Intent
from core.runtime.rails import RailsConfig, RiskRails


class Ctx:
    """Minimal controllable market context."""

    def __init__(self, bid=99.0, ask=101.0, hi=102.0, lo=98.0):
        self.bid, self.ask, self.hi, self.lo = bid, ask, hi, lo
        self.positions, self.equity = {}, 10_000.0

    def now(self):
        return datetime(2026, 1, 1, tzinfo=timezone.utc)

    def book(self, symbol):
        return self.bid, self.ask

    def prev_bar_extremes(self, symbol):
        return self.hi, self.lo


CFG = RailsConfig(leverage=20, risk_per_trade=0.01, maker_fee=0.0002,
                  taker_fee=0.0005)


# ── TakerShadow ──────────────────────────────────────────────────

def test_taker_open_long_fills_at_ask_with_entry_fee():
    eng, ctx, positions = TakerShadow(CFG), Ctx(), {}
    ev = eng.submit([Intent(action="open", symbol="X", side="long", qty=2.0,
                            stop_dist=5.0)], positions, {}, ctx)
    assert ev[0]["action"] == "LONG" and ev[0]["price"] == 101.0
    assert ev[0]["fee"] == pytest.approx(101.0 * 2 * 0.0005)
    pos = positions["X"]
    assert pos["entry"] == 101.0 and pos["stop_px"] == 96.0


def test_taker_stop_exit_fires_and_charges_exit_fee():
    eng, ctx = TakerShadow(CFG), Ctx(bid=95.0, ask=95.2)
    positions = {"X": {"side": "long", "entry": 101.0, "qty": 2.0,
                       "stop_px": 96.0, "trail_dist": None,
                       "trail_activate": None, "max_hold_ticks": None,
                       "best": 101.0, "ticks_held": 0, "tag": ""}}
    ev = eng.manage(positions, ctx)
    assert ev[0]["reason"] == "stop" and ev[0]["price"] == 96.0
    # pnl = (96-101)*2 - exit fee at stop price
    assert ev[0]["pnl"] == pytest.approx((96 - 101) * 2 - 96 * 2 * 0.0005)
    assert positions == {}


def test_taker_time_exit_after_max_hold():
    eng, ctx = TakerShadow(CFG), Ctx(bid=100.0, ask=100.2)
    positions = {"X": {"side": "long", "entry": 100.0, "qty": 1.0,
                       "stop_px": None, "trail_dist": None,
                       "trail_activate": None, "max_hold_ticks": 2,
                       "best": 100.0, "ticks_held": 0, "tag": ""}}
    assert eng.manage(positions, ctx) == []        # tick 1 of 2
    ev = eng.manage(positions, ctx)                # tick 2 -> time exit
    assert ev[0]["reason"] == "time" and positions == {}


def test_taker_trail_arms_then_fires():
    eng = TakerShadow(CFG)
    positions = {"X": {"side": "long", "entry": 100.0, "qty": 1.0,
                       "stop_px": None, "trail_dist": 2.0,
                       "trail_activate": 3.0, "max_hold_ticks": None,
                       "best": 100.0, "ticks_held": 0, "tag": ""}}
    # +1 profit: below activate -> trail not armed, no exit
    assert eng.manage(positions, Ctx(bid=101.0, ask=101.2)) == []
    # run to 105 (arms trail, best=105), then pull back through 105-2=103
    assert eng.manage(positions, Ctx(bid=105.0, ask=105.2)) == []
    ev = eng.manage(positions, Ctx(bid=102.5, ask=102.7))
    assert ev[0]["reason"] == "trail" and ev[0]["price"] == 103.0


def test_taker_close_intent_exits_at_bid():
    eng, ctx = TakerShadow(CFG), Ctx(bid=99.0, ask=101.0)
    positions = {"X": {"side": "long", "entry": 95.0, "qty": 1.0, "tag": ""}}
    ev = eng.submit([Intent(action="close", symbol="X")], positions, {}, ctx)
    assert ev[0]["price"] == 99.0 and positions == {}


# ── MakerShadow ──────────────────────────────────────────────────

def _quote(side, price, qty=1.0, **kw):
    return Intent(action="quote", symbol="X", side=side, qty=qty,
                  price=price, maker=True, **kw)


def test_maker_quote_rests_then_fills_through():
    eng, ctx = MakerShadow(CFG), Ctx(hi=102, lo=98)
    positions, quotes = {}, {}
    eng.submit([_quote("long", 99.5)], positions, quotes, ctx)
    assert "X:long" in quotes and positions == {}
    ev = eng.submit([], positions, quotes, ctx)    # prev-bar lo 98 < 99.5
    assert ev[0]["action"] == "LONG_MAKER" and ev[0]["price"] == 99.5
    assert positions["X"]["qty"] == 1.0 and quotes == {}


def test_maker_no_fill_when_bar_does_not_trade_through():
    eng, ctx = MakerShadow(CFG), Ctx(hi=102, lo=100.0)
    positions, quotes = {}, {}
    eng.submit([_quote("long", 99.5)], positions, quotes, ctx)
    assert eng.submit([], positions, quotes, ctx) == []
    assert "X:long" in quotes                      # still resting


def test_maker_same_side_fill_averages_entry():
    eng, ctx = MakerShadow(CFG), Ctx(hi=102, lo=90)
    positions = {"X": {"side": "long", "entry": 100.0, "qty": 1.0,
                       "stop_px": None, "trail_dist": None,
                       "trail_activate": None, "max_hold_ticks": None,
                       "best": 100.0, "ticks_held": 0, "tag": ""}}
    quotes = {}
    eng.submit([_quote("long", 98.0)], positions, quotes, ctx)
    ev = eng.submit([], positions, quotes, ctx)
    assert ev[0]["action"] == "LONG_MAKER_ADD"
    assert positions["X"]["qty"] == 2.0
    assert positions["X"]["entry"] == pytest.approx(99.0)


def test_maker_opposite_fill_nets_and_realizes_pnl():
    eng, ctx = MakerShadow(CFG), Ctx(hi=103, lo=90)
    positions = {"X": {"side": "long", "entry": 100.0, "qty": 2.0,
                       "stop_px": None, "trail_dist": None,
                       "trail_activate": None, "max_hold_ticks": None,
                       "best": 100.0, "ticks_held": 0, "tag": ""}}
    quotes = {}
    eng.submit([_quote("short", 102.0, qty=2.0)], positions, quotes, ctx)
    ev = eng.submit([], positions, quotes, ctx)    # hi 103 > 102 -> fills
    assert ev[0]["action"] == "NET_LONG"
    fee = 102 * 2 * 0.0002
    assert ev[0]["pnl"] == pytest.approx((102 - 100) * 2 - fee)
    assert positions == {}


def test_maker_flip_through_zero_leaves_remainder_short():
    eng, ctx = MakerShadow(CFG), Ctx(hi=103, lo=90)
    positions = {"X": {"side": "long", "entry": 100.0, "qty": 1.0,
                       "stop_px": None, "trail_dist": None,
                       "trail_activate": None, "max_hold_ticks": None,
                       "best": 100.0, "ticks_held": 0, "tag": ""}}
    quotes = {}
    eng.submit([_quote("short", 102.0, qty=3.0)], positions, quotes, ctx)
    eng.submit([], positions, quotes, ctx)
    pos = positions["X"]
    assert pos["side"] == "short" and pos["qty"] == pytest.approx(2.0)
    assert pos["entry"] == 102.0


def test_maker_quote_ttl_expires_unfilled():
    eng, ctx = MakerShadow(CFG), Ctx(hi=102, lo=100.0)   # never trades through
    positions, quotes = {}, {}
    it = _quote("long", 99.5)
    it.meta["ttl_ticks"] = 2
    eng.submit([it], positions, quotes, ctx)
    eng.submit([], positions, quotes, ctx)         # tick 1
    assert "X:long" in quotes
    eng.submit([], positions, quotes, ctx)         # tick 2 -> expires
    assert quotes == {}


def test_maker_cancel_quotes_clears_symbol():
    eng, ctx = MakerShadow(CFG), Ctx(hi=102, lo=100.0)
    positions, quotes = {}, {}
    eng.submit([_quote("long", 99.5), _quote("short", 101.5)],
               positions, quotes, ctx)
    assert len(quotes) == 2                        # dual-side quoting
    eng.submit([Intent(action="cancel_quotes", symbol="X")],
               positions, quotes, ctx)
    assert quotes == {}


# ── RiskRails ────────────────────────────────────────────────────

def test_rails_sizes_qty_from_risk_over_stop():
    rails = RiskRails(CFG)
    it = Intent(action="open", symbol="X", side="long", stop_dist=2.0)
    ok, _ = rails.check([it], equity=10_000, halted=False,
                        price_of=lambda s: 100.0)
    # qty = risk_per_trade * equity / stop = 0.01*10000/2 = 50
    assert ok[0].qty == pytest.approx(50.0)


def test_rails_clamps_stop_inside_liquidation_distance():
    rails = RiskRails(CFG)
    it = Intent(action="open", symbol="X", side="long", qty=1.0,
                stop_dist=50.0)
    ok, _ = rails.check([it], 10_000, False, price_of=lambda s: 100.0)
    # liq distance = 100 * 0.9 / 20 = 4.5 — a 50-wide stop is fiction at 20x
    assert ok[0].stop_dist == pytest.approx(4.5)


def test_rails_margin_cap_resizes_oversized_open():
    rails = RiskRails(CFG)
    it = Intent(action="open", symbol="X", side="long", qty=1_000.0,
                stop_dist=1.0)
    ok, _ = rails.check([it], 10_000, False, price_of=lambda s: 100.0)
    # margin cap 0.30*10000=3000 -> qty = 3000*20/100 = 600
    assert ok[0].qty == pytest.approx(600.0)


def test_rails_halt_blocks_opens_never_closes():
    rails = RiskRails(CFG)
    opens = [Intent(action="open", symbol="X", side="long", qty=1.0)]
    closes = [Intent(action="close", symbol="X"),
              Intent(action="open", symbol="X", side="long", qty=1.0,
                     reduce_only=True)]
    ok, rej = rails.check(opens, 10_000, True, price_of=lambda s: 100.0)
    assert ok == [] and rej[0]["reason"] == "daily_cap_halted"
    ok, rej = rails.check(closes, 10_000, True, price_of=lambda s: 100.0)
    assert len(ok) == 2 and rej == []              # risk-reducing always passes


def test_rails_min_stop_pct_floors_noise_stops():
    rails = RiskRails(RailsConfig(leverage=5, min_stop_pct=0.01))
    it = Intent(action="open", symbol="X", side="long", qty=1.0,
                stop_dist=0.1)
    ok, _ = rails.check([it], 10_000, False, price_of=lambda s: 100.0)
    assert ok[0].stop_dist == pytest.approx(1.0)   # floored at 1% of px


# ── regression: maker fills must count in backtest metrics (A1) ──

def test_backtest_metrics_count_maker_net_trades():
    from core.runtime.backtest_host import run_backtest
    from core.runtime.strategy import Strategy

    class ToyMM(Strategy):
        id, book_type, asset_domain = "toy_mm", "mm", "test"
        cadence_sec, universe, primary_tf = 60, ["X"], "1m"
        maker_only = True

        def on_tick(self, ctx):
            if ctx.positions.get("X"):
                return [Intent(action="quote", symbol="X", side="short",
                               qty=1.0, price=101.0, maker=True)]
            return [Intent(action="quote", symbol="X", side="long",
                           qty=1.0, price=99.5, maker=True)]

        def warmup_bars(self):
            return 1

    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=12, freq="1min"),
        "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0,
        "volume": 1.0})
    res = run_backtest(ToyMM(), {"X": df}, initial_equity=10_000)
    nets = [t for t in res["trades"] if t["action"].startswith("NET")]
    assert nets, "maker netting fills must occur in this fixture"
    assert res["metrics"]["total_trades"] == len(
        [t for t in res["trades"] if "pnl" in t])
    assert res["metrics"]["total_trades"] >= len(nets)


# ── regression: bar cache must not short-change larger requests (A2) ──

def test_live_bar_cache_refetches_for_larger_n():
    from core.runtime.context import LiveContext

    class FakeEx:
        calls = 0

        def fetch_ohlcv(self, sym, tf, limit):
            FakeEx.calls += 1
            base = int(pd.Timestamp("2026-01-01").timestamp() * 1000)
            return [[base + i * 60_000, 100, 101, 99, 100, 1.0]
                    for i in range(limit)]

    ctx = LiveContext(FakeEx(), primary_tf="1m")
    assert len(ctx.bars("X", "1m", 5)) == 5 and FakeEx.calls == 1
    out = ctx.bars("X", "1m", 50)                  # within 30s cache window
    assert len(out) == 50 and FakeEx.calls == 2    # refetched, not truncated
    ctx.bars("X", "1m", 10)                        # smaller: served from cache
    assert FakeEx.calls == 2
    with pytest.raises(ValueError):
        ctx.bars("X", "1m", 500)


# ── regression: state save is atomic, no tmp residue (A3) ──

def test_ledger_save_atomic_and_valid(tmp_path, monkeypatch):
    import core.paths
    monkeypatch.setattr(core.paths, "get_data_dir", lambda: tmp_path)
    from core.runtime.ledger import Ledger
    import json as _json

    led = Ledger.__new__(Ledger)
    led.sid, led.initial_equity = "t", 1000.0
    led.state_path = tmp_path / "t_state.json"
    st = {"equity": 1234.5, "positions": {}, "realized_today": 0.0}
    led.save(st)
    assert _json.loads(led.state_path.read_text())["equity"] == 1234.5
    assert not (tmp_path / "t_state.json.tmp").exists()
