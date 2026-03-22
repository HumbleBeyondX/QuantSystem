"""Telegram alert bot: sends trading alerts via Telegram Bot API."""

from __future__ import annotations

from typing import Optional

import aiohttp

from core.logging import get_logger
from dashboard.alerts.alert_manager import Alert, AlertSeverity

logger = get_logger(__name__)

SEVERITY_EMOJI = {
    AlertSeverity.INFO: "ℹ️",
    AlertSeverity.WARNING: "⚠️",
    AlertSeverity.CRITICAL: "🚨",
}


class TelegramBot:
    """Sends alerts to a Telegram chat via the Bot API."""

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_alert(self, alert: Alert) -> None:
        """Format and send an alert to Telegram."""
        emoji = SEVERITY_EMOJI.get(alert.severity, "")
        text = (
            f"{emoji} *{_escape_md(alert.title)}*\n\n"
            f"{_escape_md(alert.message)}\n\n"
            f"Source: `{alert.source}`\n"
            f"Time: `{alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        )
        await self._send_message(text, parse_mode="MarkdownV2")

    async def send_text(self, text: str) -> None:
        """Send a plain text message."""
        await self._send_message(text)

    async def _send_message(
        self, text: str, parse_mode: Optional[str] = None
    ) -> None:
        session = await self._get_session()
        url = f"{self.BASE_URL.format(token=self._token)}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("telegram_send_failed", status=resp.status, body=body)
                else:
                    logger.debug("telegram_message_sent")
        except Exception as e:
            logger.error("telegram_error", error=str(e))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)
