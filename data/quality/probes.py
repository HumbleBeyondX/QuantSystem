"""Shared synchronous source probes — used by the data-health API and the
SLA preflight gate. Sync-in-thread is deliberate: Windows asyncio DNS stalls
in the uvicorn loop (raw IPs fine, domains hang), blocking urllib in a worker
thread is the reliable pattern on this box.
"""

from __future__ import annotations

import json
import os
import urllib.request

from core.paths import get_resource_root

PROBE_TIMEOUT = 6.0


def probe_deribit() -> str:
    with urllib.request.urlopen(
            "https://www.deribit.com/api/v2/public/get_index_price"
            "?index_name=btc_usd", timeout=PROBE_TIMEOUT) as r:
        px = json.loads(r.read())["result"]["index_price"]
    if not px or px <= 0:
        raise ValueError(f"degenerate index price: {px}")
    return f"BTC index {px:,.0f}"


def probe_alpaca() -> str:
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


def probe_edgar() -> str:
    req = urllib.request.Request(
        "https://data.sec.gov/submissions/CIK0001067983.json",
        headers={"User-Agent": "QuantSystem health boxiao.huang.duke@gmail.com"})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
        name = json.loads(r.read()).get("name", "?")
    return f"EDGAR OK ({name})"


def probe_yahoo() -> str:
    req = urllib.request.Request(
        "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
        "?range=1d&interval=1d", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
        meta = json.loads(r.read())["chart"]["result"][0]["meta"]
    return f"SPY {meta.get('regularMarketPrice')}"


def probe_vps() -> str:
    import yaml
    cfg = yaml.safe_load(
        (get_resource_root() / "config" / "deployments.yaml").read_text())
    dep = next(d for d in cfg["deployments"] if d.get("enabled"))
    with urllib.request.urlopen(
            f"http://{dep['host']}:{dep['port']}/health",
            timeout=PROBE_TIMEOUT) as r:
        r.read()
    return f"{dep['name']} healthy"


PROBES = {
    "deribit": ("market_data", probe_deribit),
    "alpaca": ("market_data", probe_alpaca),
    "edgar_13f": ("alt_data", probe_edgar),
    "yahoo": ("market_data", probe_yahoo),
    "tokyo_vps": ("execution", probe_vps),
}
