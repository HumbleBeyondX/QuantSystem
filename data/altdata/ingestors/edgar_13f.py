"""EDGAR 13F-HR listener — detects NEW positions of whitelisted filers.

Strategy basis (results/pilot_13f_event_study.csv, 2022-2026):
  NEW positions of celebrity filers (weight >= 1%) drift +1.54% ex-SPY over
  the 5 trading days after disclosure (win 70%, t=3.21, n=86). Signal dies
  by day 10. Mechanism is copycat flow — fame required, conviction alone
  is not enough (Baupost/ValueAct showed nothing).

Design:
  * Whitelist tiers: A = tradeable signal, B = record-only (weak/unproven).
  * check_once(): for each filer, look for a 13F-HR accession we haven't
    seen; diff its holdings against the stored previous holdings; emit
    signal events for new CUSIPs above threshold.
  * First run auto-SEEDS (records current filings as baseline, emits
    nothing) — a fresh deploy must never fire on historical filings.
  * State + signals live under data/live_state/ (the persisted VPS volume).
  * Filing-season aware: full-rate polling only within deadline windows
    (Feb/May/Aug/Nov 14 ±); outside, one check per day is enough to catch
    early filers.

Amendments (13F-HR/A) are ignored, same as the research pipeline.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[3]
STATE_PATH = ROOT / "data" / "live_state" / "13f_monitor_state.json"
SIGNALS_PATH = ROOT / "data" / "live_state" / "13f_signals.jsonl"
FIGI_CACHE = ROOT / "data" / "altdata" / "cusip_ticker_cache.json"

UA = {"User-Agent": "QuantSystem 13F monitor boxiao.huang.duke@gmail.com"}

# Tier A: statistically validated copycat drift. Tier B: weak positives,
# record-only until they earn promotion with live data.
WHITELIST: Dict[str, Dict[str, str]] = {
    "berkshire":  {"cik": "0001067983", "tier": "A"},
    "pershing":   {"cik": "0001336528", "tier": "A"},
    "thirdpoint": {"cik": "0001040273", "tier": "A"},
    "lonepine":   {"cik": "0001061165", "tier": "B"},
    "coatue":     {"cik": "0001135730", "tier": "B"},
}

MIN_WEIGHT_SIGNAL = 0.01   # >=1% of portfolio → tradeable signal (tier A)
MIN_WEIGHT_RECORD = 0.002  # >=0.2% → recorded for research

# Quarterly 13F deadlines (45 days after quarter end, approx).
_DEADLINES = [(2, 14), (5, 15), (8, 14), (11, 14)]
WINDOW_BEFORE_D = 10
WINDOW_AFTER_D = 5


def in_filing_window(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    for m, d in _DEADLINES:
        deadline = datetime(now.year, m, d, tzinfo=timezone.utc)
        if (deadline - timedelta(days=WINDOW_BEFORE_D)
                <= now <= deadline + timedelta(days=WINDOW_AFTER_D)):
            return True
    return False


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _latest_13f(cik: str) -> Optional[Dict[str, str]]:
    """Most recent 13F-HR (not amendment) for a CIK."""
    data = json.loads(_fetch(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    recent = data["filings"]["recent"]
    best = None
    for form, acc, fdate, rdate in zip(recent["form"], recent["accessionNumber"],
                                       recent["filingDate"], recent["reportDate"]):
        if form == "13F-HR" and (best is None or fdate > best["filing_date"]):
            best = {"acc": acc, "filing_date": fdate, "report_date": rdate,
                    "entity": data.get("name", "")}
    return best


def _holdings(cik: str, acc: str) -> Dict[str, float]:
    """CUSIP -> $ value from the filing's infotable XML."""
    acc_nodash = acc.replace("-", "")
    idx = json.loads(_fetch(
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/index.json"))
    out: Dict[str, float] = {}
    for item in idx["directory"]["item"]:
        fn = item["name"]
        if not fn.lower().endswith(".xml") or "primary" in fn.lower():
            continue
        raw = _fetch(
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{fn}"
        ).decode("utf-8", "ignore")
        if "infoTable" not in raw:
            continue
        for m in re.finditer(
                r"<(?:\w+:)?cusip>([^<]+)</(?:\w+:)?cusip>.*?<(?:\w+:)?value>([\d.]+)</(?:\w+:)?value>",
                raw, re.S):
            c, v = m.group(1).strip(), float(m.group(2))
            out[c] = out.get(c, 0.0) + v
        break
    return out


def _map_ticker(cusip: str) -> str:
    """CUSIP -> ticker via OpenFIGI, cached. Empty string if not common stock."""
    cache: Dict[str, str] = {}
    if FIGI_CACHE.exists():
        cache = json.loads(FIGI_CACHE.read_text())
    if cusip in cache:
        return cache[cusip]
    try:
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": cusip}]).encode()
        req = urllib.request.Request("https://api.openfigi.com/v3/mapping",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        d = (resp[0].get("data") or [{}])[0]
        tk = d.get("ticker") if (d.get("securityType") or "") in (
            "Common Stock", "ADR", "REIT") else ""
        cache[cusip] = tk or ""
        FIGI_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FIGI_CACHE.write_text(json.dumps(cache, indent=1))
        return cache[cusip]
    except Exception as e:
        logger.warning("openfigi_lookup_failed", cusip=cusip, error=str(e))
        return ""


class Edgar13FMonitor:
    """Stateful monitor. State survives restarts via JSON on the data volume."""

    def __init__(self) -> None:
        self.state: Dict[str, Any] = {"filers": {}, "last_full_check": None}
        if STATE_PATH.exists():
            self.state = json.loads(STATE_PATH.read_text())

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self.state, indent=1))

    def _emit(self, event: Dict[str, Any]) -> None:
        SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SIGNALS_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")
        log = logger.critical if event["tier"] == "A" else logger.info
        log("NEW_13F_POSITION", **{k: v for k, v in event.items()})

    def check_once(self, force: bool = False) -> List[Dict[str, Any]]:
        """Poll every whitelisted filer once. Returns emitted signal events.

        Outside filing windows, runs at most once per ~20h unless force=True.
        """
        now = datetime.now(timezone.utc)
        if not force and not in_filing_window(now):
            last = self.state.get("last_full_check")
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 20 * 3600:
                logger.debug("13f_check_skipped_offseason")
                return []

        emitted: List[Dict[str, Any]] = []
        for name, info in WHITELIST.items():
            try:
                latest = _latest_13f(info["cik"])
                time.sleep(0.4)  # EDGAR fair use
            except Exception as e:
                logger.warning("13f_fetch_failed", filer=name, error=str(e))
                continue
            if latest is None:
                continue

            fstate = self.state["filers"].get(name, {})
            if latest["acc"] == fstate.get("acc"):
                continue  # nothing new

            try:
                holdings = _holdings(info["cik"], latest["acc"])
            except Exception as e:
                logger.warning("13f_holdings_failed", filer=name, error=str(e))
                continue
            if not holdings:
                continue

            prev = fstate.get("holdings")
            if prev is None:
                # First sighting of this filer → SEED baseline, no signals.
                logger.info("13f_seeded", filer=name, acc=latest["acc"],
                            n_holdings=len(holdings))
            else:
                total = sum(holdings.values())
                for cusip, val in holdings.items():
                    weight = val / total if total > 0 else 0.0
                    if cusip in prev or weight < MIN_WEIGHT_RECORD:
                        continue
                    ticker = _map_ticker(cusip)
                    event = {
                        "detected_at": now.isoformat(),
                        "filer": name,
                        "tier": info["tier"] if weight >= MIN_WEIGHT_SIGNAL else "record",
                        "actionable": bool(info["tier"] == "A"
                                           and weight >= MIN_WEIGHT_SIGNAL
                                           and ticker),
                        "cusip": cusip,
                        "ticker": ticker,
                        "weight_pct": round(weight * 100, 2),
                        "value_usd": val,
                        "filing_date": latest["filing_date"],
                        "report_date": latest["report_date"],
                        "acc": latest["acc"],
                    }
                    self._emit(event)
                    emitted.append(event)

            self.state["filers"][name] = {
                "acc": latest["acc"],
                "filing_date": latest["filing_date"],
                "holdings": holdings,
            }
            self._save()

        self.state["last_full_check"] = now.isoformat()
        self._save()
        return emitted
