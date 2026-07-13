"""Daily report scheduler — fires the email at configured UTC time each day.

Design
------
A minimal asyncio task that wakes up once per minute, checks whether the
configured send-time has been crossed for a day that has not yet had a
report sent, and triggers ``DailyReportGenerator.build_and_send()``.

State is kept in memory:
  * ``_last_sent_date`` — the ISO date (UTC) of the last successful send,
    used to prevent duplicate sends within the same day if the scheduler
    loop re-fires for any reason.

This is deliberately simple — no external scheduler, no cron, no DB.
When the dashboard backend restarts, the task restarts with fresh state
but still honors the once-per-day guard via ``_last_sent_date``.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from core.logging import get_logger
from core.paths import get_config_path
from dashboard.alerts.daily_report import DailyReportGenerator

logger = get_logger(__name__)

CONFIG_PATH = get_config_path("reports.yaml")


class DailyReportScheduler:
    """Single-instance daily report scheduler."""

    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = config_path
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._last_sent_date: Optional[date] = self._load_last_sent()
        self._last_run_ts: Optional[datetime] = None
        self._last_error: Optional[str] = None

    # persistence: without this, every backend restart after send-time
    # re-triggered the catch-up send (duplicate daily emails, 2026-07-12)
    _STATE_PATH = None  # set lazily to avoid import-order issues

    def _state_path(self) -> Path:
        from core.paths import get_data_dir
        return get_data_dir() / "report_scheduler_state.json"

    def _load_last_sent(self) -> Optional[date]:
        try:
            import json
            p = self._state_path()
            if p.exists():
                raw = json.loads(p.read_text()).get("last_sent_date")
                return date.fromisoformat(raw) if raw else None
        except Exception:
            pass
        return None

    def _persist_last_sent(self) -> None:
        try:
            import json
            p = self._state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "last_sent_date": (self._last_sent_date.isoformat()
                                   if self._last_sent_date else None)}))
        except Exception as e:
            logger.warning("report_scheduler_persist_failed", error=str(e))

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path) as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("daily_report", {})
        except Exception as e:
            logger.error("report_scheduler_config_load_failed", error=str(e))
            return {}

    def _target_hour_minute(self, cfg: dict) -> tuple[int, int]:
        raw = str(cfg.get("send_time_utc", "22:00")).strip()
        try:
            h, m = raw.split(":")
            return int(h) % 24, int(m) % 60
        except Exception:
            logger.warning("report_scheduler_bad_time", value=raw, fallback="22:00")
            return 22, 0

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        logger.info("report_scheduler_started")
        try:
            while not self._stop_event.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    logger.error("report_scheduler_tick_error", error=str(e))
                    self._last_error = str(e)

                # Wait 60s or until stop signal
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.info("report_scheduler_stopped")

    async def _tick(self) -> None:
        cfg = self._load_config()
        if not cfg.get("enabled", True):
            return

        now = datetime.now(timezone.utc)
        target_h, target_m = self._target_hour_minute(cfg)

        today = now.date()
        target_dt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)

        # Only fire if:
        #   - we haven't sent today
        #   - and now is at/after the target time on this day
        if self._last_sent_date == today:
            return

        if now < target_dt:
            return

        # Don't fire more than 30 min late (if scheduler started after target)
        if (now - target_dt).total_seconds() > 1800 and self._last_sent_date is None:
            # First run of the day already past window — skip, try tomorrow
            logger.info("report_scheduler_skipping_stale_window",
                        target=target_dt.isoformat(), now=now.isoformat())
            self._last_sent_date = today
            self._persist_last_sent()
            return

        logger.info("report_scheduler_firing", target=target_dt.isoformat())
        ok = await self._run_report_once()
        self._last_run_ts = now
        if ok:
            self._last_sent_date = today
            self._persist_last_sent()
            self._last_error = None
        else:
            # Retry on next tick; don't mark today as sent
            logger.warning("report_scheduler_send_failed_will_retry")

    async def _run_report_once(self) -> bool:
        try:
            gen = DailyReportGenerator.from_config()
            return await gen.build_and_send()
        except Exception as e:
            logger.error("report_scheduler_build_failed", error=str(e))
            self._last_error = str(e)
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    # ------------------------------------------------------------------
    # Manual trigger (for API endpoints)
    # ------------------------------------------------------------------
    async def trigger_now(self) -> bool:
        logger.info("report_scheduler_manual_trigger")
        ok = await self._run_report_once()
        self._last_run_ts = datetime.now(timezone.utc)
        if ok and not self._last_sent_date:
            self._last_sent_date = datetime.now(timezone.utc).date()
            self._persist_last_sent()
        return ok

    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------
    def status(self) -> dict:
        cfg = self._load_config()
        target_h, target_m = self._target_hour_minute(cfg)
        return {
            "running": bool(self._task and not self._task.done()),
            "enabled": bool(cfg.get("enabled", False)),
            "send_time_utc": f"{target_h:02d}:{target_m:02d}",
            "recipient": cfg.get("recipient", ""),
            "last_sent_date": self._last_sent_date.isoformat() if self._last_sent_date else None,
            "last_run_at": self._last_run_ts.isoformat() if self._last_run_ts else None,
            "last_error": self._last_error,
        }


# ────────────────────────────────────────────────────────────
# Module-level singleton
# ────────────────────────────────────────────────────────────

_scheduler: Optional[DailyReportScheduler] = None


def get_scheduler() -> DailyReportScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DailyReportScheduler()
    return _scheduler
