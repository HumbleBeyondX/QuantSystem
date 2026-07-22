"""Unit tests: OptionsStrangleShadow settle math + rails options passthrough.

Offline (Deribit index mocked) — the live smoke is the daily 16:55
shadow run; these lock the deterministic mechanics.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from core.runtime.intents import Intent
from core.runtime.options_fills import OptionsStrangleShadow
from core.runtime.rails import RailsConfig, RiskRails


class FakeCtx:
    def __init__(self, now_ms):
        self._now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    def now(self):
        return self._now


def _pos(kp=60000, kc=70000, mult=0.75, credit=500.0, expiry_ms=1_000):
    return {"kind": "strangle", "put": "BTC-P", "call": "BTC-C",
            "kp": kp, "kc": kc, "mult": mult, "credit_total": credit,
            "expiry_ms": expiry_ms}


def test_settle_otm_keeps_full_credit():
    eng = OptionsStrangleShadow(RailsConfig())
    positions = {"BTC-STRANGLE": _pos()}
    with patch("core.runtime.options_fills.btc_index", return_value=65_000):
        events = eng.manage(positions, FakeCtx(now_ms=2_000))
    assert len(events) == 1 and events[0]["action"] == "SETTLE"
    assert events[0]["pnl"] == 500.0          # intrinsic 0 -> full credit
    assert positions == {}


def test_settle_itm_put_pays_intrinsic():
    eng = OptionsStrangleShadow(RailsConfig())
    positions = {"BTC-STRANGLE": _pos(kp=60000, mult=1.0, credit=500.0)}
    with patch("core.runtime.options_fills.btc_index", return_value=58_000):
        events = eng.manage(positions, FakeCtx(now_ms=2_000))
    # intrinsic = (60000-58000)*1.0 = 2000; pnl = 500 - 2000
    assert events[0]["pnl"] == -1500.0
    assert events[0]["intrinsic"] == 2000.0


def test_no_settle_before_expiry():
    eng = OptionsStrangleShadow(RailsConfig())
    positions = {"BTC-STRANGLE": _pos(expiry_ms=10_000_000)}
    events = eng.manage(positions, FakeCtx(now_ms=2_000))
    assert events == [] and "BTC-STRANGLE" in positions


def test_open_books_spec_without_fee_double_count():
    eng = OptionsStrangleShadow(RailsConfig())
    positions = {}
    it = Intent(action="open", symbol="BTC-STRANGLE", side="short", qty=0.75,
                meta={"kind": "strangle", "engine": "options",
                      "put": "P", "call": "C", "kp": 60000, "kc": 70000,
                      "expiry_ms": 9e12, "mult": 0.75, "entry_index": 65000,
                      "fees": 30.0, "credit_total": 470.0})
    events = eng.submit([it], positions, {}, FakeCtx(now_ms=1_000))
    assert positions["BTC-STRANGLE"]["credit_total"] == 470.0
    # equity must move only at settlement: no "fee"/"pnl" keys on OPEN
    assert "fee" not in events[0] and "pnl" not in events[0]


def test_rails_pass_options_intent_untouched_but_respect_halt():
    rails = RiskRails(RailsConfig())
    it = Intent(action="open", symbol="BTC-STRANGLE", side="short", qty=0.75,
                meta={"kind": "strangle", "engine": "options"})
    ok, rej = rails.check([it], equity=100_000, halted=False,
                          price_of=lambda s: None)
    assert ok == [it] and it.qty == 0.75      # not resized, no stop injected
    ok, rej = rails.check([it], equity=100_000, halted=True,
                          price_of=lambda s: None)
    assert ok == [] and rej[0]["reason"] == "daily_cap_halted"
