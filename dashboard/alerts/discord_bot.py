"""Discord alert bot: sends trading alerts via Discord webhooks."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp

from core.logging import get_logger
from dashboard.alerts.alert_manager import Alert, AlertSeverity

logger = get_logger(__name__)

SEVERITY_COLOR = {
    AlertSeverity.INFO: 0x3B82F6,      # blue
    AlertSeverity.WARNING: 0xF59E0B,    # amber
    AlertSeverity.CRITICAL: 0xEF4444,   # red
}


class DiscordBot:
    """Sends alerts to a Discord channel via webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_alert(self, alert: Alert) -> None:
        """Format and send an alert as a Discord embed."""
        embed = self._build_embed(alert)
        await self._send_webhook({"embeds": [embed]})

    async def send_text(self, text: str) -> None:
        """Send a plain text message."""
        await self._send_webhook({"content": text})

    def _build_embed(self, alert: Alert) -> Dict[str, Any]:
        """Build a Discord embed from an alert."""
        fields: List[Dict[str, Any]] = [
            {"name": "Source", "value": f"`{alert.source}`", "inline": True},
            {"name": "Severity", "value": alert.severity.value.upper(), "inline": True},
        ]
        for key, value in alert.metadata.items():
            fields.append({
                "name": key.replace("_", " ").title(),
                "value": str(value),
                "inline": True,
            })

        return {
            "title": alert.title,
            "description": alert.message,
            "color": SEVERITY_COLOR.get(alert.severity, 0x808080),
            "fields": fields,
            "timestamp": alert.timestamp.isoformat(),
            "footer": {"text": "QuantSystem Alert"},
        }

    async def _send_webhook(self, payload: Dict[str, Any]) -> None:
        session = await self._get_session()
        try:
            async with session.post(self._webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    logger.error("discord_send_failed", status=resp.status, body=body)
                else:
                    logger.debug("discord_message_sent")
        except Exception as e:
            logger.error("discord_error", error=str(e))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
