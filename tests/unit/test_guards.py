"""Unit tests for data/quality/guards.py — Week 1-3 data layer upgrade."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from data.quality import guards  # noqa: E402
from data.quality.guards import (  # noqa: E402
    chain_sanity, ensure_bars_valid, ensure_utc_naive,
    pit_join, record_data_manifest, to_utc_naive,
)


# ── timestamps ──

def test_to_utc_naive_preserves_intraday():
    idx = pd.DatetimeIndex(["2026-07-01 04:00", "2026-07-01 08:00"],
                           tz="UTC")
    out = to_utc_naive(idx)
    assert out.tz is None
    assert out[1].hour == 8          # intraday hour survives


def test_to_utc_naive_converts_timezone():
    ts = pd.Timestamp("2026-07-01 20:00", tz="America/New_York")
    out = to_utc_naive(ts)
    assert out == pd.Timestamp("2026-07-02 00:00")   # 20:00 EDT = 00:00 UTC


def test_ensure_utc_naive_normalizes_daily():
    idx = pd.DatetimeIndex(["2026-07-01 00:00"], tz="America/New_York")
    out = ensure_utc_naive(idx)
    assert out[0] == pd.Timestamp("2026-07-01")      # midnight, tz-naive


# ── bar panel guard + ledger ──

def test_bars_valid_dupes_gap_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(guards, "_LEDGER_PATH", tmp_path / "ledger.jsonl")
    idx = (pd.date_range("2026-01-01", periods=10)
           .append(pd.DatetimeIndex(["2026-01-10"]))          # duplicate
           .append(pd.date_range("2026-02-01", periods=5)))   # 22d gap
    df = pd.DataFrame({"close": range(16)}, index=idx)
    clean, rep = ensure_bars_valid(df, "t", stale_after_days=None)
    assert rep.dropped_dupes == 1
    assert any("gap" in i for i in rep.issues)
    events = [json.loads(l) for l in
              (tmp_path / "ledger.jsonl").read_text().splitlines()]
    kinds = {e["kind"] for e in events}
    assert {"dropped_duplicates", "gap"} <= kinds
    assert all(e["source"] == "t" for e in events)


def test_bars_valid_raises_on_all_nan():
    df = pd.DataFrame({"close": [np.nan] * 10},
                      index=pd.date_range("2026-01-01", periods=10))
    with pytest.raises(ValueError):
        ensure_bars_valid(df, "t", stale_after_days=None)


# ── chain sanity ──

def test_chain_sanity_catches_zero_chain():
    bad = pd.DataFrame({"openInterest": [0] * 50,
                        "impliedVolatility": [0] * 50})
    assert not chain_sanity(bad, "X").ok


def test_chain_sanity_passes_healthy():
    rng = np.random.RandomState(1)
    good = pd.DataFrame({"openInterest": rng.randint(1, 999, 50),
                         "impliedVolatility": rng.uniform(.2, .6, 50)})
    assert chain_sanity(good, "X").ok


# ── PIT join ──

def test_pit_join_no_lookahead():
    """A right-row stamped day T must NOT be visible on day T with lag=1."""
    left = pd.DataFrame(index=pd.date_range("2026-01-01", periods=5))
    right = pd.DataFrame({"sig": [10.0]},
                         index=pd.DatetimeIndex(["2026-01-03"]))
    out = pit_join(left, right, lag_days=1)
    assert np.isnan(out.loc["2026-01-03", "sig_pit"])     # not yet known
    assert out.loc["2026-01-04", "sig_pit"] == 10.0        # known next day
    assert out.loc["2026-01-05", "sig_pit"] == 10.0        # persists (as-of)


def test_pit_join_takes_latest_available():
    left = pd.DataFrame(index=pd.date_range("2026-01-01", periods=6))
    right = pd.DataFrame({"sig": [1.0, 2.0]},
                         index=pd.DatetimeIndex(["2026-01-01", "2026-01-04"]))
    out = pit_join(left, right, lag_days=1)
    assert out.loc["2026-01-03", "sig_pit"] == 1.0
    assert out.loc["2026-01-05", "sig_pit"] == 2.0


# ── manifests ──

def test_manifest_hashes_inputs(tmp_path):
    f = tmp_path / "input.csv"
    f.write_text("a,b\n1,2\n")
    m1 = record_data_manifest([f], "unit_test")
    data = json.loads(m1.read_text())
    assert data["tag"] == "unit_test"
    assert data["inputs"][0]["sha256"]
    # same bytes → same hash
    m2 = record_data_manifest([f], "unit_test")
    assert (json.loads(m2.read_text())["inputs"][0]["sha256"]
            == data["inputs"][0]["sha256"])
    m1.unlink(); m2.unlink()


# ── Week-4: SLA gate ──

def test_sla_preflight_blocks_only_critical(monkeypatch):
    from data.quality import sla
    monkeypatch.setattr(sla, "load_sla", lambda: {"sources": {
        "srcA": {"critical": True, "max_latency_ms": 5000},
        "srcB": {"critical": False, "max_latency_ms": 5000}}})
    def fake_probe(sid, spec):
        if sid == "srcB":
            return sla.SLABreach(sid, spec["critical"], "down")
        return None
    monkeypatch.setattr(sla, "_probe_source", fake_probe)
    ok, why = sla.preflight(["srcA", "srcB"], strategy="t")
    assert ok  # non-critical breach does NOT block
    def fake_probe2(sid, spec):
        return sla.SLABreach(sid, spec["critical"], "down") if sid == "srcA" else None
    monkeypatch.setattr(sla, "_probe_source", fake_probe2)
    ok2, why2 = sla.preflight(["srcA"], strategy="t")
    assert not ok2 and "srcA" in why2  # critical breach blocks OPEN


def test_sla_preflight_fails_open_on_gate_error(monkeypatch):
    from data.quality import sla
    monkeypatch.setattr(sla, "check_sources",
                        lambda only=None: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, why = sla.preflight(["deribit"])
    assert ok and "gate_error" in why  # broken gate must not halt trading


def test_sla_latency_breach(monkeypatch):
    from data.quality import sla
    import data.quality.probes as probes
    import time as _t
    slow = ("market_data", lambda: _t.sleep(0.05) or "ok")
    monkeypatch.setitem(probes.PROBES, "slowsrc", slow)
    monkeypatch.setattr(sla, "load_sla", lambda: {"sources": {
        "slowsrc": {"critical": True, "max_latency_ms": 1}}})
    res = sla.check_sources()
    assert not res.ok and "latency" in res.breaches[0].detail


# ── watchdog: weekend-aware scheduler freshness ──

def test_watchdog_weekend_allowance():
    from datetime import datetime, timezone
    from core.services.uptime_watchdog import scheduler_max_age_h
    fri = datetime(2026, 7, 10, 8, 30, tzinfo=timezone.utc)   # Fri run
    sun = datetime(2026, 7, 12, 2, 0, tzinfo=timezone.utc)    # Sun morning
    # gamma (weekday-only): Fri->Sun window contains Sat+Sun => 30+48h
    assert scheduler_max_age_h("gamma_scalp", fri, sun) == 78.0
    age_h = (sun - fri).total_seconds() / 3600                # ~41.5h
    assert age_h <= 78.0                                      # weekend: FRESH
    # midweek gap is still caught: Tue->Thu, no weekend days
    tue = datetime(2026, 7, 7, 8, 30, tzinfo=timezone.utc)
    thu = datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    assert scheduler_max_age_h("gamma_scalp", tue, thu) == 30.0
    assert (thu - tue).total_seconds() / 3600 > 30.0          # midweek: DEAD
    # wheel trades 7 days — no weekend allowance ever
    assert scheduler_max_age_h("btc_wheel", fri, sun) == 30.0


def test_watchdog_scheduler_check_live():
    """Against the REAL gamma jsonl right now: must not false-alarm on weekends."""
    from core.services.uptime_watchdog import UptimeWatchdog
    wd = UptimeWatchdog()
    res = wd._check_scheduler("gamma_scalp")
    from datetime import datetime
    if res is not None and datetime.now().weekday() in (5, 6, 0):
        assert res is True   # weekend/Monday-pre-run must be fresh


# ── validation suite (fee stress / walk-forward / leave-one-out) ──

def _mk_equity(daily_rets):
    import pandas as pd
    idx = pd.date_range("2021-01-01", periods=len(daily_rets), freq="D")
    return pd.Series((1 + pd.Series(daily_rets, index=idx)).cumprod() * 100_000)


def test_loo_catches_lottery_year():
    """A curve whose edge lives in ONE year must fail leave-one-out."""
    import numpy as np
    from backtesting.validation import leave_one_out
    rng = np.random.RandomState(7)
    rets = list(rng.normal(0.0, 0.01, 365))          # 2021: pure noise
    rets += list(rng.normal(0.004, 0.01, 365))       # 2022: the lottery year
    rets += list(rng.normal(0.0, 0.01, 365))         # 2023: noise
    rep = leave_one_out(_mk_equity(rets))
    assert rep["passed"] is False                    # drops >50% ex-2022


def test_wf_and_loo_pass_steady_edge():
    import numpy as np
    from backtesting.validation import leave_one_out, walk_forward
    rng = np.random.RandomState(11)
    eq = _mk_equity(rng.normal(0.0015, 0.01, 1200))  # steady modest edge
    assert walk_forward(eq)["passed"] is True
    assert leave_one_out(eq)["passed"] is True


# ── data catalog (data/catalog_api.py) ──

def _tmp_catalog(tmp_path, monkeypatch):
    """Catalog rooted in tmp_path; ledger + manifests redirected too."""
    import data.catalog_api as catalog_api
    monkeypatch.setattr(guards, "_LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(catalog_api, "record_data_manifest",
                        lambda paths, tag: tmp_path / f"manifest_{tag}.json")
    return catalog_api.DataCatalog(root=tmp_path / "catalog")


def _bars(n=6, tz="America/New_York"):
    idx = pd.date_range("2026-07-01 09:00", periods=n, freq="4h", tz=tz)
    return pd.DataFrame({
        "timestamp": idx, "Open": 1.0, "High": 2.0, "Low": 0.5,
        "Close": np.linspace(1, 2, n), "Volume": 100.0})


def test_catalog_roundtrip_preserves_rows_and_normalizes_tz(
        tmp_path, monkeypatch):
    cat = _tmp_catalog(tmp_path, monkeypatch)
    df = _bars(6).sample(frac=1, random_state=0)      # shuffled on purpose
    cat.write(df, "crypto", "TST_USDT", "4h", source="unit")
    out = cat.read("crypto", "TST_USDT", "4h")
    assert len(out) == 6                              # rows preserved
    assert list(out.columns) == ["timestamp", "open", "high", "low",
                                 "close", "volume"]   # lowercased
    ts = pd.DatetimeIndex(out["timestamp"])
    assert ts.tz is None                              # UTC-naive
    assert ts.is_monotonic_increasing                 # sorted
    # 09:00 EDT == 13:00 UTC — intraday hour converted, not stripped
    assert ts[0] == pd.Timestamp("2026-07-01 13:00")


def test_catalog_index_entry_has_sha256(tmp_path, monkeypatch):
    cat = _tmp_catalog(tmp_path, monkeypatch)
    entry = cat.write(_bars(4), "crypto", "TST_USDT", "4h", source="unit")
    assert len(entry["sha256"]) == 64
    on_disk = json.loads((tmp_path / "catalog" / "index.json").read_text())
    e = on_disk["crypto/TST_USDT/4h"]
    assert e["sha256"] == entry["sha256"]
    assert e["rows"] == 4 and e["source"] == "unit"
    assert "quality" in e                             # bar-report summary


def test_catalog_duplicate_write_updates_not_duplicates(
        tmp_path, monkeypatch):
    cat = _tmp_catalog(tmp_path, monkeypatch)
    e1 = cat.write(_bars(4), "crypto", "TST_USDT", "4h", source="unit")
    e2 = cat.write(_bars(8), "crypto", "TST_USDT", "4h", source="unit2")
    assert len(cat.list()) == 1                       # updated, not appended
    got = cat.list()[0]
    assert got["rows"] == 8 and got["source"] == "unit2"
    assert got["sha256"] != e1["sha256"]              # new bytes, new hash
    assert e2["sha256"] == got["sha256"]


def test_catalog_dedups_timestamps_and_ledgers_it(tmp_path, monkeypatch):
    cat = _tmp_catalog(tmp_path, monkeypatch)
    df = pd.concat([_bars(5), _bars(5).tail(2)])      # 2 duplicate stamps
    cat.write(df, "crypto", "DUP_USDT", "4h", source="unit")
    assert len(cat.read("crypto", "DUP_USDT", "4h")) == 5
    events = [json.loads(l) for l in
              (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "dropped_duplicates" and
               e["source"] == "catalog_DUP_USDT_4h" for e in events)


def test_catalog_export_bars_csv_matches_nautilus_contract(
        tmp_path, monkeypatch):
    cat = _tmp_catalog(tmp_path, monkeypatch)
    cat.write(_bars(4), "crypto", "TST_USDT", "4h", source="unit")
    out = cat.export_bars_csv("crypto", "TST_USDT", "4h",
                              tmp_path / "export.csv")
    head = out.read_text().splitlines()[0]
    assert head == "timestamp,open,high,low,close,volume"
    assert len(out.read_text().splitlines()) == 5     # header + 4 bars


def test_fee_stress_breakeven():
    import numpy as np, pandas as pd
    from backtesting.validation import fee_stress
    rng = np.random.RandomState(3)
    noise = rng.normal(0, 0.01, 1000)
    def run(mult):
        # edge 15bp/day, cost 6bp/day * mult -> breakeven at 2.5x
        rets = 0.0015 - 0.0006 * mult + noise
        idx = pd.date_range("2022-01-01", periods=1000, freq="D")
        return pd.Series((1 + pd.Series(rets, index=idx)).cumprod())
    rep = fee_stress(run, multipliers=(0.5, 1, 2, 3))
    assert rep["points"][0]["sharpe"] > rep["points"][-1]["sharpe"]
    b = rep["breakeven_mult"]
    assert isinstance(b, float) and 2.0 <= b <= 3.0
    assert rep["passed"] is True
