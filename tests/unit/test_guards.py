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
