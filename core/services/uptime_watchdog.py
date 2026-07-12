"""Uptime watchdog — email alert when a deployment or scheduler goes dark.

Closes the gap exposed by the 2026-07-04 incident: the Tokyo VPS was dead
for 5 days and nothing pushed a notification (the Cloud page only knows
when someone is looking at it).

Checks every CHECK_INTERVAL:
  * each enabled deployment in config/deployments.yaml → GET /health
  * local paper schedulers (gamma/wheel daily jsonl) → file age

Alert policy: email on N consecutive failures (~15 min), ONCE per outage;
recovery email when it comes back. Never raises into the host app.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import httpx
import yaml

from core.logging import get_logger
from core.paths import get_logs_dir, get_resource_root

logger = get_logger(__name__)

CHECK_INTERVAL = 300          # 5 min
FAILS_TO_ALERT = 3            # ≈15 min of silence
SCHEDULER_MAX_AGE_H = 30      # daily jobs: >30h without a row = dead

# Schedulers whose job correctly SKIPS non-trading days (no jsonl row on
# weekends) get a 24h allowance per weekend day in the staleness window —
# otherwise every Saturday night is a false CRITICAL.
WEEKDAY_ONLY_SCHEDULERS = {"gamma_scalp"}   # btc_wheel trades 7 days


def scheduler_max_age_h(name: str, logged, now) -> float:
    """Freshness threshold for a daily scheduler, weekend-aware."""
    max_age = float(SCHEDULER_MAX_AGE_H)
    if name in WEEKDAY_ONLY_SCHEDULERS:
        day = logged.date()
        while day <= now.date():
            if day.weekday() >= 5:          # Sat/Sun in the window
                max_age += 24.0
            day += timedelta(days=1)
    return max_age

_watchdog: Optional["UptimeWatchdog"] = None


class UptimeWatchdog:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._fails: Dict[str, int] = {}
        self._alerted: Dict[str, bool] = {}
        self.last_run: Optional[str] = None

    # ── targets ──
    def _deployments(self):
        try:
            cfg = yaml.safe_load(
                (get_resource_root() / "config" / "deployments.yaml").read_text())
            return [d for d in cfg.get("deployments", []) if d.get("enabled")]
        except Exception:
            return []

    async def _check_deployment(self, dep) -> bool:
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(f"http://{dep['host']}:{dep['port']}/health")
                return r.status_code == 200
        except Exception:
            return False

    def _check_scheduler(self, name: str) -> Optional[bool]:
        """True=fresh, False=dead, None=never ran (don't alert on not-started)."""
        import json
        p = get_logs_dir() / "paper_trading" / f"{name}_daily.jsonl"
        if not p.exists():
            return None
        try:
            last = json.loads(p.read_text().strip().splitlines()[-1])
            logged = datetime.fromisoformat(last["logged_at"].replace("Z", "+00:00"))
            if logged.tzinfo is None:
                # logged_at comes from datetime.now() in the paper traders —
                # naive LOCAL time, not UTC.
                logged = logged.replace(tzinfo=datetime.now().astimezone().tzinfo)
            now = datetime.now(timezone.utc)
            age_h = (now - logged).total_seconds() / 3600
            return age_h <= scheduler_max_age_h(name, logged, now)
        except Exception:
            return None

    # ── alerting ──
    async def _notify(self, subject: str, body: str, critical: bool) -> None:
        try:
            from dashboard.alerts.alert_manager import Alert, AlertSeverity
            from dashboard.alerts.email_notifier import EmailNotifier
            n = EmailNotifier()
            if not n.is_configured:
                logger.warning("watchdog_alert_skipped_no_email", subject=subject)
                return
            await n.send_alert(Alert(
                title=subject,
                message=body,
                severity=(AlertSeverity.CRITICAL if critical
                          else AlertSeverity.INFO),
                source="uptime_watchdog",
            ))
            logger.info("watchdog_alert_sent", subject=subject)
        except Exception as e:
            logger.warning("watchdog_alert_failed", error=str(e))

    async def _record(self, key: str, ok: Optional[bool], label: str) -> None:
        if ok is None:
            return
        if ok:
            if self._alerted.get(key):
                await self._notify(
                    f"RECOVERED: {label}",
                    f"{label} is reachable again at "
                    f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}.",
                    critical=False)
            self._fails[key] = 0
            self._alerted[key] = False
        else:
            self._fails[key] = self._fails.get(key, 0) + 1
            if self._fails[key] >= FAILS_TO_ALERT and not self._alerted.get(key):
                await self._notify(
                    f"DOWN: {label}",
                    f"{label} has failed {self._fails[key]} consecutive checks "
                    f"(~{self._fails[key] * CHECK_INTERVAL // 60} min).\n"
                    f"Time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                    f"Runbook: Lightsail console (Running? CPU heartbeat?), "
                    f"browser SSH, then Reboot.",
                    critical=True)
                self._alerted[key] = True

    # ── loop ──
    async def _loop(self) -> None:
        while True:
            try:
                for dep in self._deployments():
                    ok = await self._check_deployment(dep)
                    await self._record(f"dep:{dep['id']}", ok,
                                       f"deployment {dep['name']} ({dep['host']})")
                for name in ("gamma_scalp", "btc_wheel", "btc_wheel_delta"):
                    await self._record(f"sched:{name}", self._check_scheduler(name),
                                       f"local scheduler {name}")
                self.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
            except Exception as e:
                logger.warning("watchdog_loop_error", error=str(e))
            await asyncio.sleep(CHECK_INTERVAL)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("uptime_watchdog_started",
                        interval_s=CHECK_INTERVAL, fails_to_alert=FAILS_TO_ALERT)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def status(self) -> dict:
        return {"running": bool(self._task and not self._task.done()),
                "last_run": self.last_run,
                "fail_counts": dict(self._fails),
                "active_alerts": [k for k, v in self._alerted.items() if v]}


def get_watchdog() -> UptimeWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = UptimeWatchdog()
    return _watchdog
