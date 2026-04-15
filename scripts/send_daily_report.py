#!/usr/bin/env python3
"""CLI wrapper for the daily report generator.

Examples:
    # Print a plain-text preview (no email, no browser)
    python scripts/send_daily_report.py --dry-run

    # Build and open HTML preview in browser (no email)
    python scripts/send_daily_report.py --preview

    # Actually send the email
    python scripts/send_daily_report.py --send
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import webbrowser
from pathlib import Path

# Add project root to path
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load .env
from dotenv import load_dotenv
env_path = Path(project_root) / ".env"
if env_path.exists():
    load_dotenv(env_path, override=False)

from dashboard.alerts.daily_report import (  # noqa: E402
    DailyReportGenerator,
    render_html,
    render_plain,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QuantSystem Daily Report CLI")
    parser.add_argument("--send", action="store_true", help="Send the report via email")
    parser.add_argument("--preview", action="store_true", help="Open HTML preview in browser")
    parser.add_argument("--dry-run", action="store_true", help="Print plain-text to stdout")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    gen = DailyReportGenerator.from_config()
    print(f"[INFO] Building report for recipient: {gen.recipient}", flush=True)
    data = await gen.build()

    print(
        f"[INFO] Gathered: {len(data.deployments)} deployments, "
        f"{len(data.crypto_markets)} crypto, {len(data.us_markets)} US quotes, "
        f"{len(data.risk_warnings)} risk warnings",
        flush=True,
    )

    if args.preview:
        html = render_html(data)
        tmp = Path(tempfile.gettempdir()) / "daily_report_preview.html"
        tmp.write_text(html, encoding="utf-8")
        print(f"[INFO] Preview written to: {tmp}")
        webbrowser.open(f"file://{tmp}")
        return

    if args.send:
        print("[INFO] Sending email...")
        ok = await gen.build_and_send()
        print("[SUCCESS] Email sent" if ok else "[ERROR] Email send failed — see logs")
        return

    # default: dry-run plain text
    print(render_plain(data))


if __name__ == "__main__":
    asyncio.run(main())
