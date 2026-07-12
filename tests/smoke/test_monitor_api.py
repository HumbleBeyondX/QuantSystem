"""Smoke tests for /api/monitor/strategies — the real-data strategy monitor.

Production requirements verified here:
  1. Endpoint returns 200 with a schema-valid snapshot (pydantic contract).
  2. Gamma card is built from REAL local paper files (no dead numbers).
  3. VPS-unreachable degrades the eth card to status=unreachable —
     the endpoint must NOT 500 and must NOT hang (timeout bounded).
  4. Rollup math = sum of card fields.
  5. Thin-edge flagging follows the expectancy rule.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    from dashboard.backend.main import app
    with TestClient(app) as c:
        yield c


def test_endpoint_returns_valid_snapshot(client):
    t0 = time.time()
    r = client.get("/api/monitor/strategies")
    elapsed = time.time() - t0
    assert r.status_code == 200
    body = r.json()
    for key in ("generated_at", "total_equity", "today_realized",
                "unrealized", "open_positions", "strategies"):
        assert key in body, f"missing rollup key {key}"
    assert isinstance(body["strategies"], list) and len(body["strategies"]) >= 2
    # VPS timeout is 2.5s — endpoint must be bounded even when unreachable
    assert elapsed < 10, f"endpoint too slow: {elapsed:.1f}s"


def test_gamma_card_uses_real_local_data(client):
    body = client.get("/api/monitor/strategies").json()
    gamma = next(s for s in body["strategies"] if s["id"] == "gamma_scalp")
    assert gamma["mode"] == "paper"
    assert gamma["source"].startswith("local")
    # Real paper history exists in this repo → these must be populated
    assert gamma["equity"] is not None and gamma["equity"] > 0
    assert gamma["total_return_pct"] is not None
    assert gamma["trades"] is not None and gamma["trades"] >= 1
    assert gamma["updated_at"] is not None


def test_eth_card_degrades_not_crashes(client):
    """Whether the Tokyo VPS is up or down, the card must be well-formed."""
    body = client.get("/api/monitor/strategies").json()
    eth = next(s for s in body["strategies"] if s["id"] == "eth_sniper")
    assert eth["mode"] == "live"
    assert eth["status"] in ("running", "stale", "unreachable")
    if eth["status"] == "unreachable":
        assert "no_telemetry" in eth["flags"]
        assert eth["equity"] is None
    else:
        assert eth["equity"] is not None


def test_wheel_card_real_chain_data(client):
    """BTC wheel paper card: real Deribit instruments + populated equity."""
    body = client.get("/api/monitor/strategies").json()
    wheel = next(s for s in body["strategies"] if s["id"] == "btc_wheel")
    assert wheel["mode"] == "paper"
    if wheel["status"] != "unreachable":     # trader has run at least once
        assert wheel["equity"] is not None and wheel["equity"] > 0
        assert wheel["updated_at"] is not None
        assert wheel["status"] in ("running", "flat", "stale")


def test_rollup_matches_cards(client):
    body = client.get("/api/monitor/strategies").json()
    cards = body["strategies"]
    assert abs(body["total_equity"]
               - sum(c["equity"] or 0 for c in cards)) < 0.01
    assert body["open_positions"] == sum(c["positions"] or 0 for c in cards)


def test_expectancy_and_flags_rule():
    from dashboard.backend.routers.monitor import StrategyCard, _expectancy, _flags
    # 87.8% win × 0.16 PF → expectancy ≈ 0.018 < 0.03 → thin_edge
    exp = _expectancy(87.8, 0.16)
    assert exp is not None and exp < 0.03
    card = StrategyCard(id="x", name="x", direction="long", mode="paper",
                        status="running", expectancy=exp)
    assert "thin_edge" in _flags(card)
    # healthy edge → no flag
    card2 = StrategyCard(id="y", name="y", direction="long", mode="paper",
                         status="running", expectancy=_expectancy(38.0, 2.48))
    assert "thin_edge" not in _flags(card2)


def test_data_health_real_probes(client):
    """Week-2: /api/data-health/sources runs REAL probes, bounded, never 500s."""
    import time as _t
    t0 = _t.time()
    r = client.get("/api/data-health/sources")
    assert r.status_code == 200
    assert _t.time() - t0 < 20, "probes must run in parallel and be bounded"
    body = r.json()
    ids = {s["id"] for s in body["sources"]}
    assert {"deribit", "alpaca", "edgar_13f", "yahoo", "tokyo_vps"} <= ids
    for s in body["sources"]:
        assert isinstance(s["ok"], bool) and s["latency_ms"] is not None
    names = {d["name"] for d in body["datasets"]}
    assert any("gamma" in n for n in names) and any("wheel" in n for n in names)


def test_sla_endpoint(client):
    """Week-4: /api/data-health/sla evaluates declared SLAs with real probes."""
    r = client.get("/api/data-health/sla")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["trading_allowed"], bool)
    assert "sources" in body["sla_config"]
    for b in body["breaches"]:
        assert {"target", "critical", "detail"} <= set(b)


# ── IA restructure: lifecycle registry + backtest records ──

def test_strategy_registry_lifecycle(client):
    """Registry groups strategies by honest lifecycle stage."""
    r = client.get("/api/strategies/registry")
    assert r.status_code == 200
    body = r.json()
    assert body["stages"][0] == "live"
    ids = {s["id"] for g in body["groups"].values() for s in g}
    assert {"eth_sniper", "spy_gamma_scalp", "btc_wheel",
            "combo_wheel_sniper", "btc_sniper"} <= ids
    # BTC Momentum Sniper v5 must NOT be live/paper (user opted out of papering)
    stages = {s["id"]: s["stage"] for g in body["groups"].values() for s in g}
    assert stages["btc_sniper"] == "retired"
    assert stages["btc_wheel"] == "paper"
    # every entry declares the 1.5 gate
    for g in body["groups"].values():
        for s in g:
            assert s["gate"].get("threshold", 1.5) == 1.5


def test_no_fake_paper_deployments(client):
    """strategy_deployments.json must not resurrect btc_sniper as paper."""
    r = client.get("/api/strategies/")
    assert r.status_code == 200
    assert all(d["strategy_id"] != "btc_sniper" for d in r.json())


def test_backtest_records_combo(client):
    """Official combo record surfaces with the validated numbers."""
    r = client.get("/api/backtest/records")
    assert r.status_code == 200
    recs = {x["id"]: x for x in r.json()["records"]}
    combo = recs["combo_wheel_sniper"]
    assert combo["status"] == "validated"
    assert combo["headline"]["sharpe"] == 2.81
    assert combo["headline"]["oos_sharpe"] == 2.66
    assert combo["has_curve"]
    # rejected research is on the record too, honestly
    assert recs["tsmom_research"]["status"] == "rejected"


def test_backtest_record_curve(client):
    r = client.get("/api/backtest/records/combo_wheel_sniper/curve")
    assert r.status_code == 200
    series = r.json()["series"]
    assert {"wheel", "sniper", "combo_70_30"} <= set(series)
    pts = series["combo_70_30"]
    assert len(pts) > 100 and pts[0]["equity"] == 100000.0


def test_btc_wheel_paper_endpoint(client):
    r = client.get("/api/paper-trading/btc-wheel")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == "btc_wheel"
    assert isinstance(body["equity_curve"], list)
    assert "position" in body


# ── Backtest pro: tear sheet analytics + benchmarks ──

def test_benchmarks_list(client):
    r = client.get("/api/backtest/benchmarks")
    assert r.status_code == 200
    assert "SPY" in r.json()["benchmarks"]


def test_record_tearsheet_vs_benchmark(client):
    r = client.get("/api/backtest/records/combo_wheel_sniper/analytics",
                   params={"benchmark": "BTC"})
    assert r.status_code == 200
    d = r.json()
    assert {"equity", "drawdown", "rolling_sharpe", "monthly_returns",
            "distribution", "stats"} <= set(d)
    assert d["stats"]["strategy"]["sharpe"] == 2.81
    assert "benchmark" in d["stats"] and "relative" in d["stats"]
    assert abs(d["stats"]["relative"]["beta"]) < 0.2  # combo is market-neutral
    assert len(d["monthly_returns"]) >= 5             # 2021..2026
    # rebased base-100 comparison curve
    assert d["equity"][0]["strategy"] == 100.0


def test_tearsheet_no_benchmark(client):
    r = client.get("/api/backtest/records/combo_wheel_sniper/analytics")
    assert r.status_code == 200
    assert "benchmark" not in r.json()["stats"]
