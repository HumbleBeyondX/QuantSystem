"""Daily Report API — preview, manual trigger, scheduler status.

Endpoints:
    GET  /api/reports/daily/status      — scheduler + config status
    GET  /api/reports/daily/preview     — build report and return JSON
    GET  /api/reports/daily/preview.html— build and return HTML in browser
    POST /api/reports/daily/send-now    — manually send the report now
    POST /api/reports/daily/test        — send a test email with current data
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from core.logging import get_logger
from core.services.report_scheduler import get_scheduler
from dashboard.alerts.daily_report import DailyReportGenerator, render_html, render_plain

logger = get_logger(__name__)

router = APIRouter(prefix="/api/reports/daily", tags=["Reports"])


@router.get("/status")
async def report_status():
    """Get scheduler status and configuration."""
    return get_scheduler().status()


@router.get("/preview")
async def report_preview_json():
    """Build the report and return the structured payload (no email)."""
    gen = DailyReportGenerator.from_config()
    data = await gen.build()
    return {
        "generated_at": data.generated_at.isoformat(),
        "recipient": data.recipient,
        "aggregated": data.aggregated,
        "risk_warnings": [
            {"severity": w.severity, "title": w.title, "message": w.message, "source": w.source}
            for w in data.risk_warnings
        ],
        "deployments": [
            {
                "id": d.id,
                "name": d.name,
                "online": d.online,
                "running": d.running,
                "strategy_id": d.strategy_id,
                "session_pnl": d.session_pnl,
                "session_trades": d.session_trades,
                "session_orders": d.session_orders,
                "total_value": d.total_value,
                "unrealized_pnl": d.unrealized_pnl,
                "num_positions": d.num_positions,
                "positions": [p.__dict__ for p in d.positions],
                "error": d.error,
            }
            for d in data.deployments
        ],
        "crypto_markets": [q.__dict__ for q in data.crypto_markets],
        "us_markets": [q.__dict__ for q in data.us_markets],
    }


@router.get("/preview.html", response_class=HTMLResponse)
async def report_preview_html():
    """Build and return HTML preview (opens in browser)."""
    gen = DailyReportGenerator.from_config()
    data = await gen.build()
    return HTMLResponse(render_html(data))


@router.get("/preview.txt", response_class=PlainTextResponse)
async def report_preview_text():
    """Plain-text preview."""
    gen = DailyReportGenerator.from_config()
    data = await gen.build()
    return PlainTextResponse(render_plain(data))


@router.post("/send-now")
async def report_send_now():
    """Manually trigger and send the daily report immediately.

    Production rule: a failed send must tell the operator WHY — the most
    common cause (no email credentials configured) is a config task, not a
    debugging session.
    """
    # Pre-flight: distinguish "not configured" from genuine send failures.
    try:
        from dashboard.alerts.email_notifier import EmailNotifier
        notifier = EmailNotifier()
        configured = notifier.is_configured
    except Exception:
        configured = True   # introspection must never mask the real attempt
    if not configured:
        raise HTTPException(422, detail={
            "error": "email_not_configured",
            "detail": "No email credentials found. Set ALERT_EMAIL_SENDER + "
                      "ALERT_EMAIL_PASSWORD (Gmail App Password) in .env, or "
                      "provision Gmail OAuth2 credentials, then restart the "
                      "backend.",
            "checked_env_keys": ["ALERT_EMAIL_SENDER", "ALERT_EMAIL_PASSWORD"],
        })

    scheduler = get_scheduler()
    ok = await scheduler.trigger_now()
    if not ok:
        st = scheduler.status()
        raise HTTPException(500, detail={
            "error": "report_send_failed",
            "detail": st.get("last_error") or "Report failed to build or send.",
            "scheduler_status": st,
        })
    return {"status": "sent", "recipient_info": scheduler.status()}
