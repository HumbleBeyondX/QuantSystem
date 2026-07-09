"""Real data-source health — Week-2 of the data layer upgrade.

Replaces the mtime-based fake freshness in the Data Layer page with REAL
probes: each source gets an actual API call, measured latency, and the age
of the newest data it returns. Datasets we depend on daily are checked
against their expected cadence.

GET /api/data-health/sources   → live probes (bounded, parallel, never 500s)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from core.logging import get_logger
from core.paths import get_logs_dir, get_resource_root

logger = get_logger(__name__)
router = APIRouter()

PROBE_TIMEOUT = 6.0


class SourceHealth(BaseModel):
    id: str
    kind: str                      # market_data / alt_data / execution
    ok: bool
    latency_ms: Optional[float] = None
    detail: str = ""


class DatasetFreshness(BaseModel):
    name: str
    path: str
    last_record: Optional[str] = None
    age_hours: Optional[float] = None
    expected_cadence: str = "daily"
    fresh: bool = False


class DataHealthReport(BaseModel):
    generated_at: str
    sources: List[SourceHealth]
    datasets: List[DatasetFreshness]


async def _probe(id_: str, kind: str, fn) -> SourceHealth:
    # Sync fns run in a thread: Windows asyncio DNS resolution stalls in the
    # uvicorn event loop (known box issue — raw-IP probes work, domains hang),
    # so blocking urllib in a worker thread is the reliable path here.
    t0 = time.time()
    try:
        detail = await asyncio.wait_for(asyncio.to_thread(fn),
                                        timeout=PROBE_TIMEOUT + 2)
        return SourceHealth(id=id_, kind=kind, ok=True,
                            latency_ms=round((time.time() - t0) * 1000, 1),
                            detail=detail)
    except Exception as e:
        return SourceHealth(id=id_, kind=kind, ok=False,
                            latency_ms=round((time.time() - t0) * 1000, 1),
                            detail=f"{type(e).__name__}: {e}"[:160])


def _probe_deribit() -> str:
    import urllib.request
    with urllib.request.urlopen(
            "https://www.deribit.com/api/v2/public/get_index_price"
            "?index_name=btc_usd", timeout=PROBE_TIMEOUT) as r:
        px = json.loads(r.read())["result"]["index_price"]
    return f"BTC index {px:,.0f}"


def _probe_alpaca() -> str:
    import urllib.request
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key:
        raise RuntimeError("ALPACA_API_KEY not set")
    req = urllib.request.Request(
        "https://data.alpaca.markets/v2/stocks/SPY/bars/latest",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
        ts = json.loads(r.read()).get("bar", {}).get("t", "")
    return f"SPY latest bar {ts}"


def _probe_edgar() -> str:
    import urllib.request
    req = urllib.request.Request(
        "https://data.sec.gov/submissions/CIK0001067983.json",
        headers={"User-Agent": "QuantSystem health boxiao.huang.duke@gmail.com"})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
        name = json.loads(r.read()).get("name", "?")
    return f"EDGAR OK ({name})"


def _probe_yahoo() -> str:
    import urllib.request
    req = urllib.request.Request(
        "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
        "?range=1d&interval=1d", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
        meta = json.loads(r.read())["chart"]["result"][0]["meta"]
    return f"SPY {meta.get('regularMarketPrice')}"


def _probe_vps() -> str:
    import urllib.request
    import yaml
    cfg = yaml.safe_load(
        (get_resource_root() / "config" / "deployments.yaml").read_text())
    dep = next(d for d in cfg["deployments"] if d.get("enabled"))
    with urllib.request.urlopen(
            f"http://{dep['host']}:{dep['port']}/health",
            timeout=PROBE_TIMEOUT) as r:
        r.read()
    return f"{dep['name']} healthy"


def _dataset(name: str, filename: str, ts_field: str,
             max_age_h: float) -> DatasetFreshness:
    p = get_logs_dir() / "paper_trading" / filename
    d = DatasetFreshness(name=name, path=str(p))
    try:
        last = json.loads(p.read_text().strip().splitlines()[-1])
        raw = last[ts_field]
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        d.last_record = raw
        d.age_hours = round(
            (datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)
        d.fresh = d.age_hours <= max_age_h
    except Exception as e:
        d.last_record = f"unreadable: {type(e).__name__}"
    return d


@router.get("/sources", response_model=DataHealthReport)
async def data_sources_health() -> DataHealthReport:
    """Live-probe every data source + check dependent dataset freshness."""
    sources = await asyncio.gather(
        _probe("deribit", "market_data", _probe_deribit),
        _probe("alpaca", "market_data", _probe_alpaca),
        _probe("edgar_13f", "alt_data", _probe_edgar),
        _probe("yahoo", "market_data", _probe_yahoo),
        _probe("tokyo_vps", "execution", _probe_vps),
    )
    datasets = [
        _dataset("gamma_scalp daily", "gamma_scalp_daily.jsonl",
                 "logged_at", max_age_h=30),
        _dataset("btc_wheel daily", "btc_wheel_daily.jsonl",
                 "logged_at", max_age_h=30),
    ]
    return DataHealthReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sources=list(sources),
        datasets=datasets,
    )
