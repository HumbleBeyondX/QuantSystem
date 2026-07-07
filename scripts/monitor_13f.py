#!/usr/bin/env python3
"""13F monitor CLI — run from cron (--once) or standalone (--loop).

Usage:
    python scripts/monitor_13f.py --once            # one poll (cron mode)
    python scripts/monitor_13f.py --once --force    # ignore off-season skip
    python scripts/monitor_13f.py --loop            # poll every 15 min
    python scripts/monitor_13f.py --status          # show state + recent signals

VPS cron (filing drops cluster 16:00-18:00 ET = 20:00-23:00 UTC):
    */15 20-23 * * 1-5 docker exec qs-live-trader python scripts/monitor_13f.py --once
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.altdata.ingestors.edgar_13f import (  # noqa: E402
    Edgar13FMonitor, SIGNALS_PATH, STATE_PATH, in_filing_window,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--interval", type=int, default=900)
    args = p.parse_args()

    if args.status:
        if STATE_PATH.exists():
            st = json.loads(STATE_PATH.read_text())
            print(f"last_full_check: {st.get('last_full_check')}")
            print(f"in_filing_window: {in_filing_window()}")
            for name, fs in st.get("filers", {}).items():
                print(f"  {name:12s} acc={fs.get('acc')} filed={fs.get('filing_date')} "
                      f"holdings={len(fs.get('holdings', {}))}")
        else:
            print("no state yet (first run will seed)")
        if SIGNALS_PATH.exists():
            lines = SIGNALS_PATH.read_text().strip().splitlines()
            print(f"signals recorded: {len(lines)}")
            for ln in lines[-5:]:
                e = json.loads(ln)
                print(f"  [{e['tier']}] {e['filing_date']} {e['filer']} "
                      f"{e['ticker'] or e['cusip']} w={e['weight_pct']}% "
                      f"actionable={e['actionable']}")
        return 0

    mon = Edgar13FMonitor()
    if args.loop:
        print(f"13F monitor loop, interval={args.interval}s. Ctrl+C to stop.")
        while True:
            events = mon.check_once(force=args.force)
            if events:
                print(f"⚡ {len(events)} new-position events emitted")
            time.sleep(args.interval)
    else:
        events = mon.check_once(force=args.force)
        actionable = [e for e in events if e.get("actionable")]
        print(f"check complete: {len(events)} events ({len(actionable)} actionable)")
        for e in actionable:
            print(f"  🎯 {e['filer']} NEW {e['ticker']} w={e['weight_pct']}% "
                  f"filed {e['filing_date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
