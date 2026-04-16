"""Email notification channel for risk alerts.

Supports two authentication methods (in priority order):

1. **Gmail OAuth2 (recommended)** — No password stored. Uses Google Cloud
   credentials with automatic token refresh.
   Setup:
     a. Go to https://console.cloud.google.com/apis/credentials
     b. Create an OAuth 2.0 Client ID (Desktop application)
     c. Download the JSON → save as  data/gmail_credentials.json
     d. Run:  python -m dashboard.alerts.email_notifier --setup
        This opens a browser for one-time Google login, then saves a
        refresh token to  data/gmail_token.json  (auto-refreshes forever).

2. **Gmail App Password (fallback)** — NOT your real Gmail password.
   Generate one at https://myaccount.google.com/apppasswords (requires 2FA).
   Set in .env:
     ALERT_EMAIL_SENDER=your@gmail.com
     ALERT_EMAIL_PASSWORD=xxxx-xxxx-xxxx-xxxx   (16-char app password)

Recipient (both methods):
  ALERT_EMAIL_RECIPIENT=humblebeyondx@gmail.com
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from core.logging import get_logger
from core.paths import get_data_dir, get_env_path, get_resource_root

logger = get_logger(__name__)

# ── Paths ─────────────────────────────────────────────────────────
_ENV_FILE = get_env_path()

# Gmail credentials / refresh token live in user data dir (writable,
# survives upgrades). On first run, fall back to the bundled defaults
# shipped with the dev source tree if present.
CREDENTIALS_FILE = get_data_dir() / "gmail_credentials.json"
TOKEN_FILE = get_data_dir() / "gmail_token.json"
if not CREDENTIALS_FILE.exists():
    _bundled_creds = get_resource_root() / "data" / "gmail_credentials.json"
    if _bundled_creds.exists():
        CREDENTIALS_FILE = _bundled_creds
if not TOKEN_FILE.exists():
    _bundled_token = get_resource_root() / "data" / "gmail_token.json"
    if _bundled_token.exists():
        TOKEN_FILE = _bundled_token

# Gmail API scopes (send-only — cannot read your inbox)
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


# ── Env helpers ───────────────────────────────────────────────────

def _load_env_var(key: str) -> Optional[str]:
    """Load from os.environ or .env file."""
    val = os.environ.get(key)
    if val:
        return val
    if _ENV_FILE.exists():
        try:
            with open(_ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == key:
                            return v.strip()
        except Exception:
            pass
    return None


# ── HTML template ─────────────────────────────────────────────────

SEVERITY_COLORS = {
    "info": "#3b82f6",
    "warning": "#f59e0b",
    "critical": "#ef4444",
}

SEVERITY_EMOJI = {
    "info": "\u2139\ufe0f",
    "warning": "\u26a0\ufe0f",
    "critical": "\U0001f6a8",
}


def _build_html_email(alert) -> str:
    """Build a styled HTML email body from an Alert object."""
    severity = alert.severity.value
    color = SEVERITY_COLORS.get(severity, "#6b7280")
    emoji = SEVERITY_EMOJI.get(severity, "")

    metadata_rows = ""
    if alert.metadata:
        for k, v in alert.metadata.items():
            metadata_rows += f"""
            <tr>
                <td style="padding:4px 12px;color:#9ca3af;font-size:13px;border-bottom:1px solid #374151;">{k}</td>
                <td style="padding:4px 12px;color:#e5e7eb;font-size:13px;border-bottom:1px solid #374151;">{v}</td>
            </tr>"""

    metadata_section = ""
    if metadata_rows:
        metadata_section = f"""
        <table style="width:100%;border-collapse:collapse;margin-top:16px;">
            <tr><td colspan="2" style="padding:8px 12px;color:#9ca3af;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Details</td></tr>
            {metadata_rows}
        </table>"""

    html = f"""
    <html>
    <body style="margin:0;padding:0;background-color:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
        <div style="max-width:600px;margin:20px auto;background-color:#1a1d2e;border-radius:12px;overflow:hidden;border:1px solid #2d3148;">
            <!-- Header -->
            <div style="background-color:{color};padding:16px 24px;">
                <h2 style="margin:0;color:white;font-size:18px;">
                    {emoji} QuantSystem Risk Alert
                </h2>
            </div>
            <!-- Body -->
            <div style="padding:24px;">
                <div style="margin-bottom:16px;">
                    <span style="display:inline-block;padding:4px 10px;border-radius:4px;background-color:{color}22;color:{color};font-size:12px;font-weight:600;text-transform:uppercase;">
                        {severity}
                    </span>
                    <span style="color:#6b7280;font-size:12px;margin-left:8px;">
                        {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC
                    </span>
                </div>
                <h3 style="margin:0 0 12px 0;color:#f3f4f6;font-size:20px;">{alert.title}</h3>
                <p style="margin:0 0 16px 0;color:#d1d5db;font-size:14px;line-height:1.6;">
                    {alert.message}
                </p>
                <div style="padding:12px;background-color:#111322;border-radius:8px;border-left:3px solid {color};">
                    <p style="margin:0;color:#9ca3af;font-size:12px;">Source: <strong style="color:#e5e7eb;">{alert.source}</strong></p>
                </div>
                {metadata_section}
            </div>
            <!-- Footer -->
            <div style="padding:12px 24px;background-color:#111322;border-top:1px solid #2d3148;">
                <p style="margin:0;color:#6b7280;font-size:11px;">
                    QuantSystem Auto-Alert &bull; Do not reply to this email
                </p>
            </div>
        </div>
    </body>
    </html>
    """
    return html


def _build_plain_text(alert) -> str:
    severity = alert.severity.value.upper()
    meta = "\n".join(f"  {k}: {v}" for k, v in (alert.metadata or {}).items())
    return f"""QuantSystem Risk Alert
======================
Severity: {severity}
Time: {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC
Source: {alert.source}

{alert.title}
{alert.message}

{f'Details:{chr(10)}{meta}' if meta else ''}"""


# ── OAuth2 helpers ────────────────────────────────────────────────

def _get_oauth2_credentials():
    """Load or refresh Gmail OAuth2 credentials. Returns None if not set up."""
    if not CREDENTIALS_FILE.exists() or not TOKEN_FILE.exists():
        return None

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

        # Auto-refresh if expired
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save refreshed token
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
            logger.info("gmail_oauth_token_refreshed")

        if creds and creds.valid:
            return creds
    except ImportError:
        logger.warning("gmail_oauth_deps_missing",
                       msg="Install: pip install google-auth google-auth-oauthlib google-api-python-client")
    except Exception as e:
        logger.error("gmail_oauth_error", error=str(e))
    return None


def _send_via_gmail_api(creds, recipient: str, subject: str, plain: str, html: str) -> bool:
    """Send email using Gmail API (OAuth2). Returns True on success."""
    try:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        msg = MIMEMultipart("alternative")
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()

        return True
    except Exception as e:
        logger.error("gmail_api_send_failed", error=str(e))
        return False


def setup_oauth2() -> None:
    """Interactive one-time OAuth2 setup. Opens browser for Google login.

    Run:  python -m dashboard.alerts.email_notifier --setup
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("\n[ERROR] Missing dependencies. Install them with:")
        print("  pip install google-auth google-auth-oauthlib google-api-python-client\n")
        return

    if not CREDENTIALS_FILE.exists():
        print(f"\n[ERROR] OAuth credentials file not found at:")
        print(f"  {CREDENTIALS_FILE}")
        print("\nSteps to create it:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Click '+ CREATE CREDENTIALS' → 'OAuth client ID'")
        print("  3. Application type: 'Desktop app'")
        print("  4. Download JSON → save to the path above\n")
        return

    print("\n[Gmail OAuth2 Setup]")
    print("Opening browser for Google authorization...")
    print("(Only needs 'Send Email' permission — cannot read your inbox)\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"\n[SUCCESS] Token saved to: {TOKEN_FILE}")
    print("Email alerts are now configured with OAuth2!")
    print("Your password is NOT stored — only a revocable access token.\n")


# ── EmailNotifier class ──────────────────────────────────────────

class EmailNotifier:
    """Send risk alerts via email. Tries OAuth2 first, falls back to SMTP."""

    def __init__(
        self,
        sender: Optional[str] = None,
        password: Optional[str] = None,
        recipient: Optional[str] = None,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
    ):
        self.sender = sender or _load_env_var("ALERT_EMAIL_SENDER")
        self.password = password or _load_env_var("ALERT_EMAIL_PASSWORD")
        self.recipient = recipient or _load_env_var("ALERT_EMAIL_RECIPIENT") or "humblebeyondx@gmail.com"
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port

        # Check which method is available
        self._oauth_creds = _get_oauth2_credentials()
        self._use_oauth = self._oauth_creds is not None
        self._use_smtp = bool(self.sender and self.password)
        self._configured = self._use_oauth or self._use_smtp

        if self._use_oauth:
            logger.info("email_notifier_ready", method="oauth2", recipient=self.recipient)
        elif self._use_smtp:
            logger.info("email_notifier_ready", method="smtp_app_password", recipient=self.recipient)

    @property
    def is_configured(self) -> bool:
        return self._configured

    @property
    def auth_method(self) -> str:
        if self._use_oauth:
            return "oauth2"
        if self._use_smtp:
            return "app_password"
        return "not_configured"

    async def send_alert(self, alert) -> None:
        """Async handler compatible with AlertManager.register_handler()."""
        if not self._configured:
            logger.warning(
                "email_not_configured",
                msg="Run 'python -m dashboard.alerts.email_notifier --setup' for OAuth2, "
                    "or set ALERT_EMAIL_SENDER + ALERT_EMAIL_PASSWORD in .env for App Password",
            )
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_sync, alert)

    def _send_sync(self, alert) -> None:
        """Synchronous send — tries OAuth2 first, falls back to SMTP."""
        severity = alert.severity.value.upper()
        emoji = SEVERITY_EMOJI.get(alert.severity.value, "")
        subject = f"{emoji} [{severity}] {alert.title} - QuantSystem"
        plain = _build_plain_text(alert)
        html = _build_html_email(alert)

        # Method 1: OAuth2 Gmail API
        if self._use_oauth:
            # Refresh creds if needed
            self._oauth_creds = _get_oauth2_credentials()
            if self._oauth_creds:
                ok = _send_via_gmail_api(self._oauth_creds, self.recipient, subject, plain, html)
                if ok:
                    logger.info("email_alert_sent", method="oauth2",
                                recipient=self.recipient, title=alert.title)
                    return
                logger.warning("gmail_api_failed_falling_back_to_smtp")

        # Method 2: SMTP with App Password
        if self._use_smtp:
            self._send_smtp(subject, plain, html, alert.title)
            return

        logger.error("email_send_failed_no_method_available")

    def _send_smtp(self, subject: str, plain: str, html: str, title: str) -> None:
        """Send via SMTP (App Password)."""
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = self.sender
            msg["To"] = self.recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.sender, self.password)
                server.sendmail(self.sender, [self.recipient], msg.as_string())

            logger.info("email_alert_sent", method="smtp",
                        recipient=self.recipient, title=title)
        except Exception as e:
            logger.error("email_smtp_failed", error=str(e), title=title)


# ── Singleton ─────────────────────────────────────────────────────

_default_notifier: Optional[EmailNotifier] = None


def get_email_notifier() -> EmailNotifier:
    """Get or create the default EmailNotifier instance."""
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = EmailNotifier()
    return _default_notifier


# ── CLI entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail Email Alert Setup")
    parser.add_argument("--setup", action="store_true", help="Run interactive OAuth2 setup")
    parser.add_argument("--test", action="store_true", help="Send a test email")
    parser.add_argument("--status", action="store_true", help="Check configuration status")
    args = parser.parse_args()

    if args.setup:
        setup_oauth2()

    elif args.test:
        import asyncio
        from dashboard.alerts.alert_manager import Alert, AlertSeverity
        from datetime import datetime

        notifier = get_email_notifier()
        if not notifier.is_configured:
            print("[ERROR] Email not configured. Run with --setup first, or set .env vars.")
        else:
            print(f"[INFO] Auth method: {notifier.auth_method}")
            print(f"[INFO] Sending test email to {notifier.recipient}...")
            alert = Alert(
                title="Test Alert - QuantSystem",
                message="This is a test. Your email alert system is working!",
                severity=AlertSeverity.INFO,
                source="cli_test",
                metadata={"method": notifier.auth_method, "time": datetime.utcnow().isoformat()},
            )
            asyncio.run(notifier.send_alert(alert))
            print("[DONE] Check your inbox!")

    elif args.status:
        print("\n=== Email Alert Configuration Status ===\n")
        print(f"  OAuth2 credentials:  {'FOUND' if CREDENTIALS_FILE.exists() else 'NOT FOUND'}")
        print(f"    Path: {CREDENTIALS_FILE}")
        print(f"  OAuth2 token:        {'FOUND' if TOKEN_FILE.exists() else 'NOT FOUND'}")
        print(f"    Path: {TOKEN_FILE}")
        print(f"  SMTP sender:         {_load_env_var('ALERT_EMAIL_SENDER') or 'NOT SET'}")
        print(f"  SMTP password:       {'SET' if _load_env_var('ALERT_EMAIL_PASSWORD') else 'NOT SET'}")
        print(f"  Recipient:           {_load_env_var('ALERT_EMAIL_RECIPIENT') or 'humblebeyondx@gmail.com (default)'}")

        notifier = get_email_notifier()
        print(f"\n  Active method:       {notifier.auth_method}")
        print(f"  Ready to send:       {'YES' if notifier.is_configured else 'NO'}")
        print()

    else:
        parser.print_help()
        print("\nQuick start:")
        print("  1. OAuth2 (recommended): python -m dashboard.alerts.email_notifier --setup")
        print("  2. Check status:         python -m dashboard.alerts.email_notifier --status")
        print("  3. Send test:            python -m dashboard.alerts.email_notifier --test")
