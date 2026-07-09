"""Unified strategy monitor API — real data, graceful degradation.

One endpoint aggregates every EFFECTIVE strategy into the card schema the
monitor UI renders (user rule: production-grade, no dead numbers):

  * gamma_scalp — local paper state (jsonl + pickle), always available
  * eth_sniper  — Tokyo VPS live runner, proxied with a hard timeout;
                  when the VPS is unreachable the card degrades to
                  status="unreachable" instead of breaking the page.

Design notes:
  * No blocking beyond VPS_TIMEOUT seconds (asyncio + httpx).
  * Expectancy = win_rate × profit_factor − (1 − win_rate); cards whose
    expectancy < 0.03 get a "thin_edge" flag (same rule as the mock).
  * Pydantic response models → schema is the contract for the frontend.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import httpx
import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from core.logging import get_logger
from core.paths import get_data_dir, get_logs_dir, get_resource_root

logger = get_logger(__name__)
router = APIRouter()

LOG_DIR = get_logs_dir() / "paper_trading"
DEPLOYMENTS_YAML = get_resource_root() / "config" / "deployments.yaml"
VPS_TIMEOUT = 2.5
GAMMA_INITIAL_CAPITAL = 100_000.0
THIN_EDGE = 0.03


class StrategyCard(BaseModel):
    id: str
    name: str
    direction: str                    # long / short / both / neutral
    mode: str                         # live / paper
    status: str                       # running / stale / unreachable / flat
    equity: Optional[float] = None
    today_realized: Optional[float] = None
    today_pct: Optional[float] = None
    unrealized: Optional[float] = None
    total_return_pct: Optional[float] = None
    win_rate: Optional[float] = None  # 0-100
    trades: Optional[int] = None
    profit_factor: Optional[float] = None
    expectancy: Optional[float] = None
    positions: Optional[int] = None
    flags: List[str] = []
    source: str = ""
    updated_at: Optional[str] = None
    spark: Optional[List[float]] = None   # real equity history (last ≤30 marks)


class MonitorSnapshot(BaseModel):
    generated_at: str
    total_equity: float
    today_realized: float
    unrealized: float
    open_positions: int
    strategies: List[StrategyCard]


def _expectancy(win_rate_pct: Optional[float],
                pf: Optional[float]) -> Optional[float]:
    if win_rate_pct is None or pf is None:
        return None
    w = win_rate_pct / 100.0
    return round(w * pf - (1 - w), 4)


def _flags(card: StrategyCard) -> List[str]:
    flags = []
    if card.expectancy is not None and card.expectancy < THIN_EDGE:
        flags.append("thin_edge")
    if card.status == "unreachable":
        flags.append("no_telemetry")
    return flags


# ────────────────────────────────────────────────────────────
# Gamma scalp (local paper files — the source of truth while the
# VPS scheduler is offline)
# ────────────────────────────────────────────────────────────

def _gamma_card() -> StrategyCard:
    card = StrategyCard(
        id="gamma_scalp", name="SPY Gamma Scalp (VRP)", direction="neutral",
        mode="paper", status="flat", source="local:paper_trading",
    )
    daily = LOG_DIR / "gamma_scalp_daily.jsonl"
    trades = LOG_DIR / "gamma_scalp_trades.jsonl"
    try:
        lines = daily.read_text().strip().splitlines()
        if not lines:
            return card
        last = json.loads(lines[-1])
        prev = json.loads(lines[-2]) if len(lines) > 1 else last

        card.equity = float(last["total_equity"])
        card.unrealized = float(last.get("unrealized_pnl", 0.0))
        card.today_realized = round(
            float(last["realized_pnl"]) - float(prev["realized_pnl"]), 2)
        prev_eq = float(prev["total_equity"]) or GAMMA_INITIAL_CAPITAL
        card.today_pct = round((card.equity / prev_eq - 1) * 100, 2)
        card.total_return_pct = round(
            (card.equity / GAMMA_INITIAL_CAPITAL - 1) * 100, 2)
        card.positions = 1 if last.get("position_open") else 0
        card.status = "running" if last.get("position_open") else "flat"
        card.updated_at = last.get("logged_at")
        card.spark = [round(float(json.loads(x)["total_equity"]), 2)
                      for x in lines[-30:]]

        # freshness: >4 calendar days without a row = stale scheduler
        logged = datetime.fromisoformat(last["logged_at"])
        if (datetime.now() - logged).days > 4:
            card.status = "stale"

        if trades.exists():
            t_rows = [json.loads(x) for x in
                      trades.read_text().strip().splitlines() if x]
            card.trades = len(t_rows)
            if t_rows:
                w = [t for t in t_rows if t.get("total_pnl", 0) > 0]
                card.win_rate = round(len(w) / len(t_rows) * 100, 1)
                gains = sum(t["total_pnl"] for t in w)
                losses = -sum(t["total_pnl"] for t in t_rows
                              if t.get("total_pnl", 0) <= 0)
                card.profit_factor = round(gains / losses, 2) if losses > 0 else None
    except FileNotFoundError:
        card.status = "unreachable"
        card.source = "local:missing_files"
    except Exception as e:
        logger.warning("monitor_gamma_card_failed", error=str(e))
        card.status = "unreachable"

    card.expectancy = _expectancy(card.win_rate, card.profit_factor)
    card.flags = _flags(card)
    return card


# ────────────────────────────────────────────────────────────
# ETH sniper (Tokyo VPS live runner, proxied)
# ────────────────────────────────────────────────────────────

def _vps_endpoint() -> Optional[str]:
    try:
        cfg = yaml.safe_load(DEPLOYMENTS_YAML.read_text())
        for dep in cfg.get("deployments", []):
            if dep.get("strategy_id") == "eth_sniper" and dep.get("enabled"):
                return f"http://{dep['host']}:{dep['port']}"
    except Exception as e:
        logger.warning("monitor_deployments_yaml_failed", error=str(e))
    return None


async def _eth_card() -> StrategyCard:
    card = StrategyCard(
        id="eth_sniper", name="ETH Momentum Sniper", direction="both",
        mode="live", status="unreachable", source="vps:tokyo",
    )
    base = _vps_endpoint()
    if base is None:
        card.flags = _flags(card)
        return card
    try:
        async with httpx.AsyncClient(timeout=VPS_TIMEOUT) as client:
            r = await client.get(f"{base}/api/live/runner/status")
            r.raise_for_status()
            d = r.json()
        snap = d.get("position_manager_snapshot") or {}
        card.status = "running" if d.get("running") else "stale"
        if d.get("emergency_stopped"):
            card.status = "stale"
            card.flags.append("emergency_stopped")
        card.equity = snap.get("total_value")
        card.unrealized = snap.get("unrealized_pnl")
        card.today_realized = snap.get("realized_pnl")
        eq = card.equity or 0
        if eq and snap.get("daily_pnl") is not None:
            base_eq = eq - snap["daily_pnl"]
            card.today_pct = round(snap["daily_pnl"] / base_eq * 100, 2) if base_eq else None
        card.positions = snap.get("num_positions")
        card.trades = d.get("session_trades")
        card.updated_at = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        logger.warning("monitor_eth_card_unreachable", error=str(e))
    card.expectancy = _expectancy(card.win_rate, card.profit_factor)
    card.flags = _flags(card)
    return card


# ────────────────────────────────────────────────────────────
# Endpoint
# ────────────────────────────────────────────────────────────

@router.get("/strategies", response_model=MonitorSnapshot)
async def monitor_strategies() -> MonitorSnapshot:
    """All effective strategies in monitor-card form + portfolio rollup."""
    gamma, eth = await asyncio.gather(
        asyncio.to_thread(_gamma_card), _eth_card(),
    )
    cards = [gamma, eth]
    return MonitorSnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_equity=round(sum(c.equity or 0 for c in cards), 2),
        today_realized=round(sum(c.today_realized or 0 for c in cards), 2),
        unrealized=round(sum(c.unrealized or 0 for c in cards), 2),
        open_positions=sum(c.positions or 0 for c in cards),
        strategies=cards,
    )
