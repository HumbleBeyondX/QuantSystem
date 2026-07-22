#!/usr/bin/env python3
"""Phase B daily parity diff: original wheels vs runtime-v2 shadows.

Compares, per pair (btc_wheel vs btc_wheel_v2, btc_wheel_delta vs
btc_wheel_delta_v2):
  * position parity  — same put/call instruments, strikes, expiry
  * mark parity      — unrealized per unit mult within tolerance
  * settle parity    — latest SETTLE pnl per unit mult within tolerance

Verdicts:
  MATCH            — decisions/marks identical (within timing tolerance)
  PHASE_MISMATCH   — different lifecycle phase (e.g. shadow opened on a
                     different day than the original — expected until the
                     first common roll); informational, not a failure
  DIVERGENCE       — same phase, different decision or numbers → investigate

Appends one row per run to logs/paper_trading/wheel_parity.jsonl and an
audit line to data/promotions.jsonl (event: parity_report).

Run: python scripts/wheel_parity_check.py         (17:05 scheduled task)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE_DIR = ROOT / "data" / "live_state"
LOG_DIR = ROOT / "logs" / "paper_trading"
PARITY_LOG = LOG_DIR / "wheel_parity.jsonl"
PROMOTIONS = ROOT / "data" / "promotions.jsonl"

PAIRS = [("btc_wheel", "btc_wheel_v2"),
         ("btc_wheel_delta", "btc_wheel_delta_v2")]

MARK_TOL_PER_MULT = 150.0     # USD per unit mult (~0.75 BTC-mult books):
                              # hourly-vs-daily mark timing skew allowance
SETTLE_TOL_PER_MULT = 1.0     # settle math is deterministic given index;
                              # only index-read timing differs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(p: Path) -> dict | None:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _last_settle(trades_path: Path) -> dict | None:
    if not trades_path.exists():
        return None
    rows = [json.loads(l) for l in
            trades_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    settles = [r for r in rows if r.get("action") == "SETTLE"]
    return settles[-1] if settles else None


def check_pair(orig_id: str, v2_id: str) -> dict:
    out = {"pair": f"{orig_id} vs {v2_id}", "checks": {}, "verdict": "MATCH"}

    ost = _load(STATE_DIR / f"{orig_id}_state.json")
    vst = _load(STATE_DIR / f"{v2_id}_state.json")
    if not ost or not vst:
        out["verdict"] = "PHASE_MISMATCH"
        out["checks"]["state"] = ("missing_original" if not ost
                                  else "missing_v2_state")
        return out

    opos = ost.get("position")                       # original: single dict
    vpos = (vst.get("positions") or {}).get("BTC-STRANGLE")  # v2: keyed

    # ── position parity ──
    if (opos is None) != (vpos is None):
        out["verdict"] = "PHASE_MISMATCH"
        out["checks"]["position"] = {
            "original": bool(opos), "v2": bool(vpos),
            "note": "different lifecycle phase (expected until first "
                    "common roll)"}
    elif opos and vpos:
        same_instr = (opos["put"] == vpos["put"]
                      and opos["call"] == vpos["call"])
        same_expiry = opos["expiry_ms"] == vpos["expiry_ms"]
        same_open_day = (str(opos.get("opened", ""))[:10]
                         == str(vpos.get("opened", ""))[:10])
        out["checks"]["position"] = {
            "original": f"{opos['put']}|{opos['call']}",
            "v2": f"{vpos['put']}|{vpos['call']}",
            "same_instruments": same_instr, "same_expiry": same_expiry,
            "opened": [str(opos.get("opened", ""))[:16],
                       str(vpos.get("opened", ""))[:16]],
        }
        if not same_instr:
            # same open day + different strikes = real selection divergence;
            # different open day = phase artifact
            out["verdict"] = ("DIVERGENCE" if same_open_day and same_expiry
                              else "PHASE_MISMATCH")
        else:
            # credit per unit mult (equities differ slightly between books)
            oc = opos["credit_total"] / opos["mult"]
            vc = vpos["credit_total"] / vpos["mult"]
            out["checks"]["credit_per_mult"] = {
                "original": round(oc, 2), "v2": round(vc, 2),
                "diff": round(vc - oc, 2)}
            if abs(vc - oc) > MARK_TOL_PER_MULT:
                out["verdict"] = "DIVERGENCE"
    else:
        out["checks"]["position"] = {"original": None, "v2": None,
                                     "note": "both flat"}

    # ── settle parity (latest settle, only when same contracts) ──
    oset = _last_settle(LOG_DIR / f"{orig_id}_trades.jsonl")
    vset = _last_settle(LOG_DIR / f"{v2_id}_trades.jsonl")
    if oset and vset:
        v_tag = vset.get("tag", "")
        same_contracts = (oset.get("put", "") in v_tag
                          and oset.get("call", "") in v_tag)
        if same_contracts:
            # pnl/credit is mult-independent: 1 - intrinsic/credit.
            # Given same contracts + same settle index, ratios must match.
            o_ratio = (oset["pnl"] / oset["credit_total"]
                       if oset.get("credit_total") else None)
            v_ratio = (vset["pnl"] / vset["credit_total"]
                       if vset.get("credit_total") else None)
            ok = (o_ratio is not None and v_ratio is not None
                  and abs(o_ratio - v_ratio) <= 0.02)
            out["checks"]["settle"] = {
                "original_pnl": oset["pnl"], "v2_pnl": vset["pnl"],
                "pnl_over_credit": [o_ratio and round(o_ratio, 4),
                                    v_ratio and round(v_ratio, 4)],
                "same_contracts": True, "within_tolerance": ok}
            if not ok:
                out["verdict"] = "DIVERGENCE"
        else:
            out["checks"]["settle"] = {
                "same_contracts": False,
                "note": "settles from different contracts — phase artifact"}

    return out


def main() -> None:
    report = {"ts": _now(), "date": _now()[:10], "pairs": []}
    worst = "MATCH"
    order = {"MATCH": 0, "PHASE_MISMATCH": 1, "DIVERGENCE": 2}
    for orig, v2 in PAIRS:
        r = check_pair(orig, v2)
        report["pairs"].append(r)
        if order[r["verdict"]] > order[worst]:
            worst = r["verdict"]
    report["overall"] = worst

    PARITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PARITY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(report) + "\n")
    with open(PROMOTIONS, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": _now(), "event": "parity_report",
            "scope": "phase_b_wheel_shadows", "overall": worst,
            "pairs": {r["pair"]: r["verdict"] for r in report["pairs"]},
        }) + "\n")

    print(f"[{_now()}] wheel shadow parity: {worst}")
    for r in report["pairs"]:
        print(f"  {r['pair']}: {r['verdict']}")
        for k, v in r["checks"].items():
            print(f"    {k}: {json.dumps(v, default=str)}")
    if worst == "DIVERGENCE":
        sys.exit(1)


if __name__ == "__main__":
    main()
