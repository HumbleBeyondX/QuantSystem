"""Daily Report Generator.

Builds a comprehensive end-of-day email that covers:

  1. Header — timestamp, overall portfolio health
  2. P&L Summary — aggregated across all cloud deployments
  3. Positions — current open positions per deployment
  4. Risk Assessment — forward-looking warnings (session loss, drawdown,
     VIX regime, position concentration)
  5. Market Snapshot — crypto (CCXT) + US equities (yfinance)
  6. Footer

The report re-uses the Gmail OAuth2 / SMTP pipeline from
``dashboard/alerts/email_notifier.py`` so no additional credentials
are required once that is set up.

Usage (programmatic):
    from dashboard.alerts.daily_report import DailyReportGenerator
    report = DailyReportGenerator.from_config()
    await report.build_and_send()

Usage (CLI testing):
    python -m dashboard.alerts.daily_report --preview   # write to tmp HTML
    python -m dashboard.alerts.daily_report --send      # send real email
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.logging import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "reports.yaml"
DEPLOYMENTS_CONFIG = PROJECT_ROOT / "config" / "deployments.yaml"


# ============================================================
# Data structures
# ============================================================

@dataclass
class PositionRow:
    deployment: str
    strategy: str
    symbol: str
    side: str           # "LONG" / "SHORT" / "FLAT"
    quantity: float
    avg_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: float = 0.0
    liquidation_price: float = 0.0
    asset_class: str = "crypto"


@dataclass
class MarketQuote:
    symbol: str
    label: str
    price: float
    change_24h: float       # percent
    source: str             # "ccxt" or "yfinance"
    error: Optional[str] = None


@dataclass
class RiskWarning:
    severity: str           # "info" / "warning" / "critical"
    title: str
    message: str
    source: str


@dataclass
class DeploymentSummary:
    id: str
    name: str
    online: bool
    running: bool
    strategy_id: str
    symbol: str
    asset_class: str
    session_pnl: float
    session_trades: int
    session_orders: int
    total_value: float
    unrealized_pnl: float
    realized_pnl: float
    daily_pnl: float
    num_positions: int
    positions: List[PositionRow] = field(default_factory=list)
    latency_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class ReportData:
    """Container passed to HTML template."""
    generated_at: datetime
    deployments: List[DeploymentSummary]
    aggregated: Dict[str, float]
    risk_warnings: List[RiskWarning]
    crypto_markets: List[MarketQuote]
    us_markets: List[MarketQuote]
    recipient: str


# ============================================================
# Data gatherers
# ============================================================

class DailyReportGenerator:
    """Gathers data from all sources and produces the daily email."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        deployments: Optional[List[Dict[str, Any]]] = None,
    ):
        self.config = config or {}
        self.deployments_config = deployments or []
        self.recipient: str = self.config.get(
            "recipient",
            os.environ.get("ALERT_EMAIL_RECIPIENT", "humblebeyondx@gmail.com"),
        )
        self.thresholds: Dict[str, float] = self.config.get("risk_thresholds", {})

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        report_config_path: Path = DEFAULT_CONFIG,
        deployments_config_path: Path = DEPLOYMENTS_CONFIG,
    ) -> "DailyReportGenerator":
        cfg: Dict[str, Any] = {}
        if report_config_path.exists():
            with open(report_config_path) as f:
                cfg = (yaml.safe_load(f) or {}).get("daily_report", {})

        deployments: List[Dict[str, Any]] = []
        if deployments_config_path.exists():
            with open(deployments_config_path) as f:
                d = yaml.safe_load(f) or {}
            deployments = d.get("deployments", [])

        return cls(config=cfg, deployments=deployments)

    # ------------------------------------------------------------------
    # Deployment / position gathering
    # ------------------------------------------------------------------
    async def _fetch_deployment_status(self, dep: Dict[str, Any]) -> DeploymentSummary:
        """Pull /api/live/runner/status from a remote VPS."""
        import aiohttp

        url = f"http://{dep['host']}:{dep.get('port', 8002)}/api/live/runner/status"
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        start = time.time()

        summary = DeploymentSummary(
            id=dep["id"],
            name=dep.get("name", dep["id"]),
            online=False,
            running=False,
            strategy_id=dep.get("strategy_id", ""),
            symbol=dep.get("symbol", ""),
            asset_class=dep.get("asset_class", "unknown"),
            session_pnl=0.0,
            session_trades=0,
            session_orders=0,
            total_value=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            daily_pnl=0.0,
            num_positions=0,
        )

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    summary.latency_ms = round((time.time() - start) * 1000, 1)
                    if resp.status != 200:
                        summary.error = f"HTTP {resp.status}"
                        return summary
                    data = await resp.json()
        except Exception as e:
            summary.error = str(e)
            return summary

        summary.online = True
        summary.running = bool(data.get("running"))
        summary.session_pnl = float(data.get("session_pnl", 0) or 0)
        summary.session_trades = int(data.get("session_trades", 0) or 0)
        summary.session_orders = int(data.get("session_orders", 0) or 0)

        snap = data.get("position_manager_snapshot") or {}
        summary.total_value = float(snap.get("total_value", 0) or 0)
        summary.unrealized_pnl = float(snap.get("unrealized_pnl", 0) or 0)
        summary.realized_pnl = float(snap.get("realized_pnl", 0) or 0)
        summary.daily_pnl = float(snap.get("daily_pnl", 0) or 0)
        summary.num_positions = int(snap.get("num_positions", 0) or 0)

        for pos in snap.get("positions", []) or []:
            qty = float(pos.get("quantity", 0) or 0)
            side = "FLAT" if qty == 0 else ("LONG" if qty > 0 else "SHORT")
            summary.positions.append(PositionRow(
                deployment=summary.name,
                strategy=summary.strategy_id,
                symbol=pos.get("symbol", summary.symbol),
                side=side,
                quantity=abs(qty),
                avg_price=float(pos.get("avg_price", 0) or 0),
                mark_price=float(pos.get("mark_price", 0) or 0),
                unrealized_pnl=float(pos.get("unrealized_pnl", 0) or 0),
                leverage=float(pos.get("leverage", 0) or 0),
                liquidation_price=float(pos.get("liquidation_price", 0) or 0),
                asset_class=dep.get("asset_class", "crypto"),
            ))
        return summary

    async def gather_deployments(self) -> List[DeploymentSummary]:
        enabled = [d for d in self.deployments_config if d.get("enabled", True)]
        if not enabled:
            return []
        results = await asyncio.gather(
            *[self._fetch_deployment_status(d) for d in enabled],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, DeploymentSummary)]

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def _fetch_crypto_quote_sync(self, symbol: str, label: str) -> MarketQuote:
        """Fetch crypto quote via CCXT (public ticker, no auth needed)."""
        try:
            import ccxt
            ex = ccxt.okx({"enableRateLimit": True, "timeout": 10000})
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker.get("last") or 0)
            pct = ticker.get("percentage")
            if pct is None:
                open_ = ticker.get("open")
                if open_ and price:
                    pct = ((price - float(open_)) / float(open_)) * 100
                else:
                    pct = 0.0
            return MarketQuote(
                symbol=symbol,
                label=label,
                price=price,
                change_24h=float(pct or 0),
                source="ccxt",
            )
        except Exception as e:
            logger.debug("crypto_quote_failed", symbol=symbol, error=str(e))
            return MarketQuote(symbol=symbol, label=label, price=0.0,
                               change_24h=0.0, source="ccxt", error=str(e))

    def _fetch_us_quote_sync(self, symbol: str, label: str) -> MarketQuote:
        """Fetch US market quote via yfinance."""
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            hist = t.history(period="5d", interval="1d")
            if hist.empty:
                return MarketQuote(symbol=symbol, label=label, price=0.0,
                                   change_24h=0.0, source="yfinance",
                                   error="No data")
            close = hist["Close"].values
            price = float(close[-1])
            prev = float(close[-2]) if len(close) >= 2 else price
            pct = ((price - prev) / prev * 100) if prev else 0.0
            return MarketQuote(
                symbol=symbol,
                label=label,
                price=price,
                change_24h=pct,
                source="yfinance",
            )
        except Exception as e:
            logger.debug("us_quote_failed", symbol=symbol, error=str(e))
            return MarketQuote(symbol=symbol, label=label, price=0.0,
                               change_24h=0.0, source="yfinance", error=str(e))

    async def gather_market_snapshot(
        self,
    ) -> tuple[List[MarketQuote], List[MarketQuote]]:
        markets_cfg = self.config.get("markets", {})
        crypto_cfg = markets_cfg.get("crypto", [])
        equities_cfg = markets_cfg.get("us_equities", [])

        loop = asyncio.get_event_loop()

        crypto_tasks = [
            loop.run_in_executor(None, self._fetch_crypto_quote_sync,
                                 c["symbol"], c.get("label", c["symbol"]))
            for c in crypto_cfg
        ]
        us_tasks = [
            loop.run_in_executor(None, self._fetch_us_quote_sync,
                                 e["symbol"], e.get("label", e["symbol"]))
            for e in equities_cfg
        ]

        crypto_results = await asyncio.gather(*crypto_tasks, return_exceptions=True)
        us_results = await asyncio.gather(*us_tasks, return_exceptions=True)

        crypto = [q for q in crypto_results if isinstance(q, MarketQuote)]
        us = [q for q in us_results if isinstance(q, MarketQuote)]
        return crypto, us

    # ------------------------------------------------------------------
    # Risk assessment
    # ------------------------------------------------------------------
    def assess_risk(
        self,
        deployments: List[DeploymentSummary],
        us_markets: List[MarketQuote],
    ) -> List[RiskWarning]:
        warnings: List[RiskWarning] = []
        t = self.thresholds

        # Per-deployment health
        for dep in deployments:
            if not dep.online:
                warnings.append(RiskWarning(
                    severity="critical",
                    title=f"{dep.name} is OFFLINE",
                    message=f"Cannot reach VPS ({dep.error or 'unknown error'}). "
                            f"Live position may be unmonitored.",
                    source=dep.id,
                ))
                continue

            if not dep.running:
                warnings.append(RiskWarning(
                    severity="critical",
                    title=f"{dep.name} runner STOPPED",
                    message="Runner is online but not trading. Positions may be "
                            "unmanaged. Investigate and restart ASAP.",
                    source=dep.id,
                ))

            # Session loss threshold
            loss_warn = t.get("session_loss_warning_usd", 5.0)
            loss_crit = t.get("session_loss_critical_usd", 15.0)
            if dep.session_pnl <= -loss_crit:
                warnings.append(RiskWarning(
                    severity="critical",
                    title=f"{dep.name}: session loss ${dep.session_pnl:.2f}",
                    message=f"Session P&L has breached the critical "
                            f"${loss_crit:.2f} threshold. Circuit breaker "
                            f"should have fired; verify positions.",
                    source=dep.id,
                ))
            elif dep.session_pnl <= -loss_warn:
                warnings.append(RiskWarning(
                    severity="warning",
                    title=f"{dep.name}: session loss ${dep.session_pnl:.2f}",
                    message=f"Session P&L below -${loss_warn:.2f} warning level.",
                    source=dep.id,
                ))

            # Margin utilization (leverage-adjusted position concentration)
            # For futures/perps, compare MARGIN used (notional / leverage)
            # against equity, not raw notional.
            if dep.total_value > 0 and dep.positions:
                margin_used = 0.0
                for p in dep.positions:
                    notional = abs(p.quantity * p.mark_price)
                    lev = p.leverage if p.leverage > 0 else 1.0
                    margin_used += notional / lev
                pct = margin_used / dep.total_value if dep.total_value > 0 else 0
                pos_warn = t.get("position_pct_warning", 0.80)
                pos_crit = t.get("position_pct_critical", 0.95)
                if pct >= pos_crit:
                    warnings.append(RiskWarning(
                        severity="critical",
                        title=f"{dep.name}: margin use {pct*100:.0f}% of equity",
                        message=f"Margin consumed is {pct*100:.0f}% of equity "
                                f"(threshold {pos_crit*100:.0f}%). Close to margin call.",
                        source=dep.id,
                    ))
                elif pct >= pos_warn:
                    warnings.append(RiskWarning(
                        severity="warning",
                        title=f"{dep.name}: margin use {pct*100:.0f}% of equity",
                        message=f"Margin consumed is {pct*100:.0f}% of equity "
                                f"(warning threshold {pos_warn*100:.0f}%).",
                        source=dep.id,
                    ))

            # Leverage / liquidation distance
            for pos in dep.positions:
                if pos.liquidation_price and pos.mark_price:
                    if pos.side == "LONG":
                        dist = (pos.mark_price - pos.liquidation_price) / pos.mark_price
                    else:
                        dist = (pos.liquidation_price - pos.mark_price) / pos.mark_price
                    if dist > 0 and dist < 0.05:
                        warnings.append(RiskWarning(
                            severity="critical",
                            title=f"{pos.symbol}: {dist*100:.1f}% from liquidation",
                            message=f"{pos.side} {pos.quantity} @ {pos.avg_price} — "
                                    f"liq @ {pos.liquidation_price:.2f}, "
                                    f"only {dist*100:.1f}% cushion left.",
                            source=dep.id,
                        ))

        # Market regime warnings
        vix_q = next((m for m in us_markets if "VIX" in m.symbol), None)
        if vix_q and vix_q.price > 0:
            vix_warn = t.get("vix_warning", 22)
            vix_crit = t.get("vix_critical", 30)
            if vix_q.price >= vix_crit:
                warnings.append(RiskWarning(
                    severity="critical",
                    title=f"VIX @ {vix_q.price:.1f} — high-fear regime",
                    message=f"Volatility regime exceeds {vix_crit}. Expect wider "
                            f"spreads, higher slippage, and correlated crypto drawdowns. "
                            f"Consider reducing position sizes.",
                    source="market_regime",
                ))
            elif vix_q.price >= vix_warn:
                warnings.append(RiskWarning(
                    severity="warning",
                    title=f"VIX @ {vix_q.price:.1f} — elevated volatility",
                    message=f"VIX above {vix_warn}. Monitor for regime change.",
                    source="market_regime",
                ))

        # Aggregate deployment count check
        total_eligible = len([d for d in self.deployments_config if d.get("enabled", True)])
        online = len([d for d in deployments if d.online])
        if total_eligible and online < total_eligible:
            warnings.append(RiskWarning(
                severity="warning",
                title=f"{total_eligible - online}/{total_eligible} deployments offline",
                message="One or more VPS instances unreachable.",
                source="fleet_health",
            ))

        if not warnings:
            warnings.append(RiskWarning(
                severity="info",
                title="All systems nominal",
                message="No risk thresholds breached. Deployments online, "
                        "positions within limits, market regime stable.",
                source="all_clear",
            ))

        return warnings

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------
    @staticmethod
    def aggregate(deployments: List[DeploymentSummary]) -> Dict[str, float]:
        return {
            "total_equity": sum(d.total_value for d in deployments),
            "total_session_pnl": sum(d.session_pnl for d in deployments),
            "total_unrealized": sum(d.unrealized_pnl for d in deployments),
            "total_realized": sum(d.realized_pnl for d in deployments),
            "total_daily_pnl": sum(d.daily_pnl for d in deployments),
            "total_trades": sum(d.session_trades for d in deployments),
            "total_orders": sum(d.session_orders for d in deployments),
            "num_deployments": len(deployments),
            "online_count": len([d for d in deployments if d.online]),
            "running_count": len([d for d in deployments if d.running]),
            "total_positions": sum(d.num_positions for d in deployments),
        }

    # ------------------------------------------------------------------
    # Report pipeline
    # ------------------------------------------------------------------
    async def build(self) -> ReportData:
        deployments = await self.gather_deployments()
        crypto, us = await self.gather_market_snapshot()
        warnings = self.assess_risk(deployments, us)
        agg = self.aggregate(deployments)

        return ReportData(
            generated_at=datetime.now(timezone.utc),
            deployments=deployments,
            aggregated=agg,
            risk_warnings=warnings,
            crypto_markets=crypto,
            us_markets=us,
            recipient=self.recipient,
        )

    async def build_and_send(self) -> bool:
        """Build and send the report. Returns True on success."""
        try:
            data = await self.build()
        except Exception as e:
            logger.error("daily_report_build_failed", error=str(e))
            return False

        html = render_html(data)
        plain = render_plain(data)
        subject = _subject_line(data)

        # Send via EmailNotifier — reuse auth pipeline
        from dashboard.alerts.email_notifier import get_email_notifier

        notifier = get_email_notifier()
        if not notifier.is_configured:
            logger.error("daily_report_email_not_configured")
            return False

        loop = asyncio.get_event_loop()
        sent = await loop.run_in_executor(
            None,
            _send_raw_email,
            notifier,
            self.recipient,
            subject,
            plain,
            html,
        )
        if sent:
            logger.info(
                "daily_report_sent",
                recipient=self.recipient,
                deployments=len(data.deployments),
                warnings=len(data.risk_warnings),
            )
        else:
            logger.error("daily_report_send_failed")
        return sent


# ============================================================
# Email helpers — raw send (bypasses Alert dataclass)
# ============================================================

def _subject_line(data: ReportData) -> str:
    agg = data.aggregated
    pnl = agg["total_session_pnl"]
    emoji = "\U0001f4ca"  # 📊
    if any(w.severity == "critical" for w in data.risk_warnings):
        emoji = "\U0001f6a8"  # 🚨
    elif any(w.severity == "warning" for w in data.risk_warnings):
        emoji = "\u26a0\ufe0f"  # ⚠️
    date_str = data.generated_at.strftime("%Y-%m-%d")
    sign = "+" if pnl >= 0 else ""
    return f"{emoji} QuantSystem Daily Report {date_str} — P&L {sign}${pnl:.2f}"


def _send_raw_email(
    notifier,
    recipient: str,
    subject: str,
    plain: str,
    html: str,
) -> bool:
    """Send a preformatted email via the EmailNotifier auth pipeline."""
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # OAuth path
    if notifier._use_oauth:
        try:
            from dashboard.alerts.email_notifier import _get_oauth2_credentials
            from googleapiclient.discovery import build

            creds = _get_oauth2_credentials()
            if creds:
                service = build("gmail", "v1", credentials=creds, cache_discovery=False)
                msg = MIMEMultipart("alternative")
                msg["To"] = recipient
                msg["Subject"] = subject
                msg.attach(MIMEText(plain, "plain"))
                msg.attach(MIMEText(html, "html"))
                raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
                service.users().messages().send(
                    userId="me", body={"raw": raw},
                ).execute()
                return True
        except Exception as e:
            logger.warning("daily_report_oauth_send_failed", error=str(e))

    # SMTP fallback
    if notifier._use_smtp:
        import smtplib
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = notifier.sender
            msg["To"] = recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP(notifier.smtp_host, notifier.smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(notifier.sender, notifier.password)
                server.sendmail(notifier.sender, [recipient], msg.as_string())
            return True
        except Exception as e:
            logger.error("daily_report_smtp_send_failed", error=str(e))
    return False


# ============================================================
# Render
# ============================================================

_SEVERITY_COLOR = {"info": "#3b82f6", "warning": "#f59e0b", "critical": "#ef4444"}
_SEVERITY_BG = {"info": "#1e3a8a22", "warning": "#92400e22", "critical": "#7f1d1d22"}
_SEVERITY_EMOJI = {"info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "critical": "\U0001f6a8"}


def _pnl_color(v: float) -> str:
    if v > 0:
        return "#10b981"
    if v < 0:
        return "#ef4444"
    return "#9ca3af"


def _fmt_money(v: float, decimals: int = 2) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):.{decimals}f}"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def render_html(data: ReportData) -> str:
    agg = data.aggregated
    pnl = agg["total_session_pnl"]
    pnl_color = _pnl_color(pnl)

    # ── Warnings block ──
    warnings_html = ""
    for w in data.risk_warnings:
        col = _SEVERITY_COLOR.get(w.severity, "#6b7280")
        bg = _SEVERITY_BG.get(w.severity, "#37415122")
        emoji = _SEVERITY_EMOJI.get(w.severity, "")
        warnings_html += f"""
        <div style="margin-bottom:10px;padding:12px 14px;background:{bg};border-left:3px solid {col};border-radius:6px;">
            <div style="color:{col};font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">
                {emoji} {w.severity}
            </div>
            <div style="color:#f3f4f6;font-size:14px;font-weight:600;margin-bottom:2px;">{w.title}</div>
            <div style="color:#d1d5db;font-size:13px;line-height:1.5;">{w.message}</div>
            <div style="color:#6b7280;font-size:11px;margin-top:6px;">source: {w.source}</div>
        </div>"""

    # ── Deployments ──
    deployments_html = ""
    for dep in data.deployments:
        status_color = "#10b981" if (dep.online and dep.running) else ("#f59e0b" if dep.online else "#ef4444")
        status_text = "RUNNING" if (dep.online and dep.running) else ("STOPPED" if dep.online else "OFFLINE")

        positions_rows = ""
        if dep.positions:
            for pos in dep.positions:
                pnl_col = _pnl_color(pos.unrealized_pnl)
                side_col = "#10b981" if pos.side == "LONG" else ("#ef4444" if pos.side == "SHORT" else "#6b7280")
                positions_rows += f"""
                <tr>
                    <td style="padding:6px 10px;color:#e5e7eb;font-size:13px;border-bottom:1px solid #2d3148;">{pos.symbol}</td>
                    <td style="padding:6px 10px;color:{side_col};font-size:13px;font-weight:600;border-bottom:1px solid #2d3148;">{pos.side}</td>
                    <td style="padding:6px 10px;color:#e5e7eb;font-size:13px;border-bottom:1px solid #2d3148;text-align:right;">{pos.quantity:.4f}</td>
                    <td style="padding:6px 10px;color:#9ca3af;font-size:13px;border-bottom:1px solid #2d3148;text-align:right;">${pos.avg_price:.2f}</td>
                    <td style="padding:6px 10px;color:#e5e7eb;font-size:13px;border-bottom:1px solid #2d3148;text-align:right;">${pos.mark_price:.2f}</td>
                    <td style="padding:6px 10px;color:{pnl_col};font-size:13px;font-weight:600;border-bottom:1px solid #2d3148;text-align:right;">{_fmt_money(pos.unrealized_pnl, 4)}</td>
                </tr>"""
            positions_block = f"""
            <table style="width:100%;border-collapse:collapse;margin-top:8px;">
                <tr>
                    <th style="padding:6px 10px;color:#6b7280;font-size:11px;text-align:left;font-weight:400;text-transform:uppercase;">Symbol</th>
                    <th style="padding:6px 10px;color:#6b7280;font-size:11px;text-align:left;font-weight:400;text-transform:uppercase;">Side</th>
                    <th style="padding:6px 10px;color:#6b7280;font-size:11px;text-align:right;font-weight:400;text-transform:uppercase;">Qty</th>
                    <th style="padding:6px 10px;color:#6b7280;font-size:11px;text-align:right;font-weight:400;text-transform:uppercase;">Avg</th>
                    <th style="padding:6px 10px;color:#6b7280;font-size:11px;text-align:right;font-weight:400;text-transform:uppercase;">Mark</th>
                    <th style="padding:6px 10px;color:#6b7280;font-size:11px;text-align:right;font-weight:400;text-transform:uppercase;">uPnL</th>
                </tr>
                {positions_rows}
            </table>"""
        else:
            positions_block = """
            <div style="margin-top:10px;padding:10px 12px;background:#111322;border-radius:6px;color:#6b7280;font-size:13px;text-align:center;">
                No open positions
            </div>"""

        sess_col = _pnl_color(dep.session_pnl)

        deployments_html += f"""
        <div style="margin-bottom:16px;padding:16px;background:#1a1d2e;border-radius:10px;border:1px solid #2d3148;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <div>
                    <div style="color:#f3f4f6;font-size:16px;font-weight:600;">{dep.name}</div>
                    <div style="color:#6b7280;font-size:12px;margin-top:2px;">{dep.strategy_id} · {dep.asset_class}</div>
                </div>
                <span style="padding:4px 10px;border-radius:12px;background:{status_color}22;color:{status_color};font-size:11px;font-weight:600;">
                    {status_text}
                </span>
            </div>
            <table style="width:100%;border-collapse:collapse;">
                <tr>
                    <td style="padding:4px 0;color:#6b7280;font-size:11px;text-transform:uppercase;width:25%;">Session P&amp;L</td>
                    <td style="padding:4px 0;color:{sess_col};font-size:13px;font-weight:600;width:25%;">{_fmt_money(dep.session_pnl, 4)}</td>
                    <td style="padding:4px 0;color:#6b7280;font-size:11px;text-transform:uppercase;width:25%;">Equity</td>
                    <td style="padding:4px 0;color:#e5e7eb;font-size:13px;width:25%;">${dep.total_value:.2f}</td>
                </tr>
                <tr>
                    <td style="padding:4px 0;color:#6b7280;font-size:11px;text-transform:uppercase;">Trades</td>
                    <td style="padding:4px 0;color:#e5e7eb;font-size:13px;">{dep.session_trades}</td>
                    <td style="padding:4px 0;color:#6b7280;font-size:11px;text-transform:uppercase;">Positions</td>
                    <td style="padding:4px 0;color:#e5e7eb;font-size:13px;">{dep.num_positions}</td>
                </tr>
                <tr>
                    <td style="padding:4px 0;color:#6b7280;font-size:11px;text-transform:uppercase;">Orders</td>
                    <td style="padding:4px 0;color:#e5e7eb;font-size:13px;">{dep.session_orders}</td>
                    <td style="padding:4px 0;color:#6b7280;font-size:11px;text-transform:uppercase;">Latency</td>
                    <td style="padding:4px 0;color:#e5e7eb;font-size:13px;">{f"{dep.latency_ms} ms" if dep.latency_ms else "—"}</td>
                </tr>
            </table>
            {positions_block}
        </div>"""

    # ── Market snapshot table ──
    def _market_rows(quotes: List[MarketQuote]) -> str:
        rows = ""
        for q in quotes:
            if q.error:
                rows += f"""
                <tr>
                    <td style="padding:6px 10px;color:#e5e7eb;font-size:13px;border-bottom:1px solid #2d3148;">{q.label}</td>
                    <td style="padding:6px 10px;color:#6b7280;font-size:12px;border-bottom:1px solid #2d3148;">{q.symbol}</td>
                    <td style="padding:6px 10px;color:#6b7280;font-size:13px;border-bottom:1px solid #2d3148;text-align:right;" colspan="2">unavailable</td>
                </tr>"""
                continue
            col = _pnl_color(q.change_24h)
            rows += f"""
            <tr>
                <td style="padding:6px 10px;color:#e5e7eb;font-size:13px;border-bottom:1px solid #2d3148;">{q.label}</td>
                <td style="padding:6px 10px;color:#6b7280;font-size:12px;border-bottom:1px solid #2d3148;">{q.symbol}</td>
                <td style="padding:6px 10px;color:#e5e7eb;font-size:13px;border-bottom:1px solid #2d3148;text-align:right;">${q.price:,.2f}</td>
                <td style="padding:6px 10px;color:{col};font-size:13px;font-weight:600;border-bottom:1px solid #2d3148;text-align:right;">{_fmt_pct(q.change_24h)}</td>
            </tr>"""
        return rows

    crypto_block = f"""
        <h3 style="color:#f3f4f6;margin:16px 0 8px;font-size:14px;">Crypto Markets (24h)</h3>
        <table style="width:100%;border-collapse:collapse;background:#1a1d2e;border-radius:8px;overflow:hidden;">
            {_market_rows(data.crypto_markets) or '<tr><td style="padding:10px;color:#6b7280;" colspan="4">No data</td></tr>'}
        </table>""" if data.crypto_markets else ""

    us_block = f"""
        <h3 style="color:#f3f4f6;margin:16px 0 8px;font-size:14px;">US Markets (daily change)</h3>
        <table style="width:100%;border-collapse:collapse;background:#1a1d2e;border-radius:8px;overflow:hidden;">
            {_market_rows(data.us_markets) or '<tr><td style="padding:10px;color:#6b7280;" colspan="4">No data</td></tr>'}
        </table>""" if data.us_markets else ""

    ts = data.generated_at.strftime("%Y-%m-%d %H:%M UTC")

    return f"""
    <html>
    <body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
        <div style="max-width:720px;margin:20px auto;background:#0f1117;">

            <div style="background:linear-gradient(135deg,#1e3a8a,#5b21b6);padding:20px 24px;border-radius:12px 12px 0 0;">
                <h1 style="margin:0;color:white;font-size:20px;">\U0001f4ca QuantSystem Daily Report</h1>
                <div style="color:rgba(255,255,255,0.8);font-size:12px;margin-top:4px;">{ts}</div>
            </div>

            <div style="background:#12152a;padding:20px 24px;border-left:1px solid #2d3148;border-right:1px solid #2d3148;">

                <div style="display:flex;gap:16px;margin-bottom:20px;">
                    <div style="flex:1;padding:14px;background:#1a1d2e;border-radius:10px;border:1px solid #2d3148;">
                        <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Session P&amp;L</div>
                        <div style="color:{pnl_color};font-size:22px;font-weight:700;margin-top:4px;">{_fmt_money(pnl, 4)}</div>
                    </div>
                    <div style="flex:1;padding:14px;background:#1a1d2e;border-radius:10px;border:1px solid #2d3148;">
                        <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Total Equity</div>
                        <div style="color:#f3f4f6;font-size:22px;font-weight:700;margin-top:4px;">${agg['total_equity']:.2f}</div>
                    </div>
                    <div style="flex:1;padding:14px;background:#1a1d2e;border-radius:10px;border:1px solid #2d3148;">
                        <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:1px;">Deployments</div>
                        <div style="color:#f3f4f6;font-size:22px;font-weight:700;margin-top:4px;">{agg['running_count']}/{agg['num_deployments']}</div>
                        <div style="color:#6b7280;font-size:11px;margin-top:2px;">running</div>
                    </div>
                </div>

                <h2 style="color:#f3f4f6;margin:0 0 12px;font-size:16px;">\U0001f4cb Risk Assessment</h2>
                {warnings_html}

                <h2 style="color:#f3f4f6;margin:20px 0 12px;font-size:16px;">\U0001f3af Active Deployments</h2>
                {deployments_html or '<div style="padding:14px;color:#6b7280;background:#1a1d2e;border-radius:8px;">No deployments configured</div>'}

                <h2 style="color:#f3f4f6;margin:20px 0 12px;font-size:16px;">\U0001f30e Market Snapshot</h2>
                {crypto_block}
                {us_block}

            </div>

            <div style="background:#111322;padding:14px 24px;border:1px solid #2d3148;border-top:none;border-radius:0 0 12px 12px;">
                <p style="margin:0;color:#6b7280;font-size:11px;text-align:center;">
                    QuantSystem Daily Report · generated {ts}<br/>
                    Delivered to {data.recipient} · reply-to this email is not monitored
                </p>
            </div>
        </div>
    </body>
    </html>
    """


def render_plain(data: ReportData) -> str:
    agg = data.aggregated
    ts = data.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    lines: List[str] = []
    lines.append("QuantSystem Daily Report")
    lines.append("=" * 50)
    lines.append(f"Generated: {ts}")
    lines.append("")
    lines.append(f"Session P&L:     {_fmt_money(agg['total_session_pnl'], 4)}")
    lines.append(f"Total Equity:    ${agg['total_equity']:.2f}")
    lines.append(f"Deployments:     {agg['running_count']}/{agg['num_deployments']} running")
    lines.append(f"Open Positions:  {agg['total_positions']}")
    lines.append("")
    lines.append("-- Risk Assessment --")
    for w in data.risk_warnings:
        lines.append(f"[{w.severity.upper()}] {w.title}")
        lines.append(f"  {w.message}")
        lines.append("")
    lines.append("-- Deployments --")
    for dep in data.deployments:
        status = "RUNNING" if (dep.online and dep.running) else ("STOPPED" if dep.online else "OFFLINE")
        lines.append(f"  {dep.name} [{status}] — {dep.strategy_id}")
        lines.append(f"    Session P&L: {_fmt_money(dep.session_pnl, 4)} | Equity: ${dep.total_value:.2f} | Trades: {dep.session_trades}")
        for p in dep.positions:
            lines.append(f"    {p.side} {p.quantity} {p.symbol} @ {p.avg_price:.2f} (mark {p.mark_price:.2f}, uPnL {_fmt_money(p.unrealized_pnl, 4)})")
        lines.append("")
    lines.append("-- Crypto Markets --")
    for q in data.crypto_markets:
        if q.error:
            lines.append(f"  {q.label:20s} {q.symbol:12s} unavailable")
        else:
            lines.append(f"  {q.label:20s} {q.symbol:12s} ${q.price:>10,.2f}  {_fmt_pct(q.change_24h)}")
    lines.append("")
    lines.append("-- US Markets --")
    for q in data.us_markets:
        if q.error:
            lines.append(f"  {q.label:20s} {q.symbol:12s} unavailable")
        else:
            lines.append(f"  {q.label:20s} {q.symbol:12s} ${q.price:>10,.2f}  {_fmt_pct(q.change_24h)}")
    lines.append("")
    lines.append("-- End of Report --")
    return "\n".join(lines)


# ============================================================
# CLI entry
# ============================================================

if __name__ == "__main__":
    import argparse
    import tempfile
    import webbrowser

    parser = argparse.ArgumentParser(description="QuantSystem Daily Report")
    parser.add_argument("--preview", action="store_true",
                        help="Build report and open HTML locally (no email)")
    parser.add_argument("--send", action="store_true",
                        help="Build and send the report via email")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and print plain-text report")
    args = parser.parse_args()

    async def _main():
        gen = DailyReportGenerator.from_config()
        data = await gen.build()
        if args.preview:
            html = render_html(data)
            tmp = Path(tempfile.gettempdir()) / "daily_report_preview.html"
            tmp.write_text(html, encoding="utf-8")
            print(f"Preview written to {tmp}")
            webbrowser.open(f"file://{tmp}")
        elif args.send:
            ok = await gen.build_and_send()
            print("Sent" if ok else "FAILED — see logs")
        else:
            print(render_plain(data))

    asyncio.run(_main())
