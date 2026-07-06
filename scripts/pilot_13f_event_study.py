#!/usr/bin/env python3
"""Pilot: 13F disclosure-day event study — do NEW positions of conviction
funds drift after the filing becomes public?

Unbiased design: pull ALL 13F-HR filings for chosen filers from EDGAR,
diff consecutive infotables to find every NEW CUSIP (no cherry-picking),
then measure stock returns from the first tradable moment (next open
after filing) over +5/+10/+20 trading days vs SPY.

If stock-level drift exists, a call-option overlay is simulated with the
gamma-scalp BS engine (crude but indicative of leverage/theta trade-off).
"""
from __future__ import annotations
import json, re, sys, time
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

UA = {"User-Agent": "QuantSystem research boxiao.huang.duke@gmail.com"}

FILERS = {
    "berkshire": "0001067983",   # Berkshire Hathaway
    "pershing":  "0001336528",   # Pershing Square
    "appaloosa": "0001656456",   # Appaloosa (Tepper) - newer CIK
    "thirdpoint": "0001040273",  # Third Point (Loeb)
    "duquesne":  "0001536411",   # Duquesne Family Office (Druckenmiller)
    "scion":     "0001649339",   # Scion Asset Management (Burry)
    # ── expansion batch (verify entity names on fetch!) ──
    "baupost":   "0001061768",   # Baupost Group (Klarman)
    "greenlight": "0001079114",  # Greenlight Capital (Einhorn)
    "lonepine":  "0001061165",   # Lone Pine Capital
    "viking":    "0001103804",   # Viking Global
    "tiger":     "0001167483",   # Tiger Global
    "coatue":    "0001135730",   # Coatue Management
    "icahn":     "0000921669",   # Icahn (Carl C.)
    "valueact":  "0001418814",   # ValueAct Capital
}

FIGI_CACHE = ROOT / "data" / "altdata" / "cusip_ticker_cache.json"

def map_cusips_openfigi(cusips: list[str]) -> dict[str, str]:
    """CUSIP -> ticker via OpenFIGI free API (batches of 10, cached)."""
    cache = {}
    if FIGI_CACHE.exists():
        cache = json.loads(FIGI_CACHE.read_text())
    todo = [c for c in cusips if c not in cache]
    for i in range(0, len(todo), 10):
        batch = todo[i:i+10]
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in batch]).encode()
        req = urllib.request.Request(
            "https://api.openfigi.com/v3/mapping", data=body,
            headers={"Content-Type": "application/json"})
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            for c, r in zip(batch, resp):
                d = (r.get("data") or [{}])[0]
                # only US common stock / ADR — skip ETFs, notes, preferred
                sec_type = (d.get("securityType") or "")
                tk = d.get("ticker")
                cache[c] = tk if tk and sec_type in ("Common Stock", "ADR", "REIT") else ""
        except Exception as e:
            print(f"  openfigi batch failed ({e}); waiting 25s...")
            time.sleep(25)
            continue
        time.sleep(3)  # free tier: 25 req/min
    FIGI_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FIGI_CACHE.write_text(json.dumps(cache, indent=1))
    return {c: t for c, t in cache.items() if t}

# CUSIP(6-digit issuer prefix) -> ticker, hand-mapped as they appear.
CUSIP6_TICKER = {
    "171232": "CB",    "422806": "HEI",  "90384S": "ULTA", "257867": "DPZ",
    "73278L": "POOL",  "21036P": "STZ",  "674599": "OXY",  "40434L": "HPQ",
    "690768": "OMC",   "874054": "TSM",  "165167": "CHTR", "12662P": "CVX",
    "092113": "BKR",   "580135": "MCK",  "532457": "LEN",  "20030N": "CMCSA",
    "716973": "PFE",   "444859": "HUM",  "91324P": "UNH",  "02079K": "GOOGL",
    "023135": "AMZN",  "637071": "NSC",  "802481": "SAIA", "05470P": "AXTA",
    "500754": "KHC",   "84265V": "SO",   "60505104": "BAC",
    "125523": "CI",    "58933Y": "MRK",  "654106": "NKE",  "88160R": "TSLA",
    "747525": "QCOM",  "00287Y": "ABBV", "09857L": "BKNG", "68389X": "ORCL",
    "92826C": "V",     "57636Q": "MA",   "438128": "HON",  "89400J07": "TPR",
    "029912": "APO",   "03662Q": "ANSS", "88579Y": "MMM",  "532716": "LYV",
    "98980L": "ZBH",   "760759": "RSG",  "82968B": "SIRI", "254687": "DIS",
}

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def get_13f_filings(cik: str) -> list[dict]:
    data = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    print(f"  CIK {cik} = {data.get('name', '?')}")  # entity-name sanity check
    recent = data["filings"]["recent"]
    out = []
    for form, acc, fdate, rdate in zip(recent["form"], recent["accessionNumber"],
                                       recent["filingDate"], recent["reportDate"]):
        if form == "13F-HR":  # exclude amendments (13F-HR/A) to avoid lookahead mess
            out.append({"acc": acc, "filing_date": fdate, "report_date": rdate})
    return sorted(out, key=lambda x: x["report_date"])

def get_holdings(cik: str, acc: str) -> dict[str, float]:
    """CUSIP -> total $ value from the infotable XML."""
    acc_nodash = acc.replace("-", "")
    idx = json.loads(fetch(
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/index.json"))
    xml_files = [f["name"] for f in idx["directory"]["item"]
                 if f["name"].lower().endswith(".xml") and "primary" not in f["name"].lower()]
    holdings: dict[str, float] = {}
    for fn in xml_files:
        raw = fetch(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{fn}").decode("utf-8", "ignore")
        if "<infoTable" not in raw and "<ns1:infoTable" not in raw:
            continue
        for m in re.finditer(
                r"<(?:\w+:)?cusip>([^<]+)</(?:\w+:)?cusip>.*?<(?:\w+:)?value>([\d.]+)</(?:\w+:)?value>",
                raw, re.S):
            cusip, val = m.group(1).strip(), float(m.group(2))
            holdings[cusip] = holdings.get(cusip, 0.0) + val
        break
    return holdings

def main():
    events = []
    for filer, cik in FILERS.items():
        try:
            filings = [f for f in get_13f_filings(cik) if f["report_date"] >= "2022-01-01"]
        except Exception as e:
            print(f"  {filer}: EDGAR list failed: {e}")
            continue
        print(f"{filer}: {len(filings)} filings {filings[0]['report_date']} → {filings[-1]['report_date']}")
        prev = None
        for f in filings:
            try:
                hold = get_holdings(cik, f["acc"])
                time.sleep(0.3)
            except Exception as e:
                print(f"  {f['acc']}: fetch failed {e}"); prev = None; continue
            if prev is not None and hold:
                total = sum(hold.values())
                for cusip, val in hold.items():
                    if cusip not in prev and val / total > 0.002:  # >0.2% of portfolio
                        events.append({
                            "filer": filer, "cusip": cusip, "cusip6": cusip[:6],
                            "filing_date": f["filing_date"], "pct_port": val / total,
                        })
            prev = hold
    ev = pd.DataFrame(events)
    if ev.empty:
        print("No events found."); return
    print(f"\nMapping {ev['cusip'].nunique()} CUSIPs via OpenFIGI...")
    figi_map = map_cusips_openfigi(sorted(ev["cusip"].unique()))
    ev["ticker"] = ev["cusip"].map(figi_map)
    n_unmapped = ev["ticker"].isna().sum() + (ev["ticker"] == "").sum()
    ev = ev[ev["ticker"].notna() & (ev["ticker"] != "")]
    # Dedupe multi-shareclass rows (e.g. GOOG + GOOGL same filer same day)
    ev = ev.sort_values("pct_port", ascending=False).drop_duplicates(
        subset=["filer", "ticker", "filing_date"])
    print(f"NEW-position events: {len(ev)} mapped, {n_unmapped} skipped (ETF/unmapped)")

    # ── Event study via yfinance ──
    import yfinance as yf
    spy = yf.Ticker("SPY").history(start="2022-01-01")["Close"]
    spy.index = spy.index.tz_localize(None)
    rows = []
    for _, e in ev.iterrows():
        try:
            px = yf.Ticker(e["ticker"]).history(start="2022-01-01")["Close"]
            px.index = px.index.tz_localize(None)
        except Exception:
            continue
        fd = pd.Timestamp(e["filing_date"])
        after = px.index[px.index > fd]          # first close AFTER filing date
        if len(after) < 21:
            continue
        t0 = after[0]
        p0 = px.loc[t0]
        s0 = spy.loc[spy.index >= t0].iloc[0]
        row = {"filer": e["filer"], "ticker": e["ticker"],
               "filing_date": e["filing_date"], "pct_port": round(e["pct_port"]*100, 2)}
        for h in (5, 10, 20):
            t_h = after[h] if len(after) > h else after[-1]
            r_stock = px.loc[t_h] / p0 - 1
            r_spy = spy.loc[spy.index >= t_h].iloc[0] / s0 - 1
            row[f"exc_{h}d"] = (r_stock - r_spy) * 100
        rows.append(row)
    res = pd.DataFrame(rows)
    print("\n" + "=" * 90)
    print("EVENT STUDY — excess return vs SPY after 13F disclosure (entry: first close after filing)")
    print("=" * 90)
    print(res.to_string(index=False))
    print("-" * 90)
    for h in (5, 10, 20):
        col = res[f"exc_{h}d"]
        tstat = col.mean() / (col.std() / np.sqrt(len(col))) if len(col) > 2 else float("nan")
        print(f"  +{h:2d}d: mean {col.mean():+.2f}%  median {col.median():+.2f}%  "
              f"win {(col > 0).mean()*100:.0f}%  t={tstat:.2f}  n={len(col)}")
    res.to_csv(ROOT / "results" / "pilot_13f_event_study.csv", index=False)
    print(f"\nSaved → results/pilot_13f_event_study.csv")

if __name__ == "__main__":
    main()
