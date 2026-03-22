"""Unusual options activity detection.

Monitors options flow for large sweeps, unusual volume/OI ratios,
and institutional positioning to generate trading signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List
from uuid import uuid4

from core.enums import SignalDirection
from core.logging import get_logger
from core.models import OptionContract, Signal

logger = get_logger(__name__)


@dataclass
class OptionsFlow:
    """Single options trade from the flow feed."""
    symbol: str
    underlying: str
    strike: float
    expiry: str
    option_type: str  # "CALL" | "PUT"
    side: str         # "BUY" | "SELL"
    size: int
    price: float
    premium: float    # size * price * multiplier
    timestamp: datetime
    is_sweep: bool = False  # Multi-exchange sweep
    at_ask: bool = False    # Filled at or above ask (aggressive buy)


class UnusualActivityDetector:
    """Detects unusual options activity patterns."""

    def __init__(
        self,
        volume_threshold_mult: float = 3.0,
        premium_threshold: float = 100_000,
        sweep_threshold: int = 5,
    ) -> None:
        self.volume_threshold_mult = volume_threshold_mult
        self.premium_threshold = premium_threshold
        self.sweep_threshold = sweep_threshold

        # State
        self._avg_volume: Dict[str, float] = {}  # symbol -> avg daily volume
        self._daily_flow: Dict[str, List[OptionsFlow]] = {}
        self._alerts: List[Dict] = []

    def process_flow(self, flow: OptionsFlow) -> List[Dict]:
        """Process a single options flow event and detect anomalies."""
        alerts: List[Dict] = []
        underlying = flow.underlying

        # Track daily flow
        self._daily_flow.setdefault(underlying, []).append(flow)

        # Alert 1: Large premium trade
        if flow.premium > self.premium_threshold:
            alert = {
                "type": "large_premium",
                "underlying": underlying,
                "details": f"{flow.option_type} {flow.strike} {flow.expiry}",
                "premium": flow.premium,
                "size": flow.size,
                "side": flow.side,
                "is_sweep": flow.is_sweep,
                "at_ask": flow.at_ask,
                "timestamp": flow.timestamp,
            }
            alerts.append(alert)
            logger.info("unusual_options_activity", **alert)

        # Alert 2: Sweep order (same option hit across multiple exchanges)
        if flow.is_sweep and flow.at_ask:
            alerts.append({
                "type": "aggressive_sweep",
                "underlying": underlying,
                "details": f"{flow.side} {flow.option_type} {flow.strike} {flow.expiry}",
                "premium": flow.premium,
                "size": flow.size,
                "timestamp": flow.timestamp,
            })

        self._alerts.extend(alerts)
        return alerts

    def get_sentiment(self, underlying: str) -> Dict:
        """Compute options sentiment for an underlying from daily flow."""
        flows = self._daily_flow.get(underlying, [])
        if not flows:
            return {"call_premium": 0, "put_premium": 0, "ratio": 0, "sentiment": "neutral"}

        call_premium = sum(f.premium for f in flows if f.option_type == "CALL" and f.at_ask)
        put_premium = sum(f.premium for f in flows if f.option_type == "PUT" and f.at_ask)
        total = call_premium + put_premium

        if total == 0:
            ratio = 0
        else:
            ratio = call_premium / total

        sentiment = "neutral"
        if ratio > 0.65:
            sentiment = "bullish"
        elif ratio < 0.35:
            sentiment = "bearish"

        return {
            "call_premium": call_premium,
            "put_premium": put_premium,
            "ratio": ratio,
            "sentiment": sentiment,
            "total_premium": total,
            "flow_count": len(flows),
        }

    def generate_signals(self, strategy_id: str) -> List[Signal]:
        """Generate signals from unusual activity patterns."""
        signals: List[Signal] = []
        now = datetime.utcnow()

        # Group recent alerts by underlying
        alert_counts: Dict[str, Dict] = {}
        for alert in self._alerts[-50:]:
            underlying = alert.get("underlying", "")
            if underlying not in alert_counts:
                alert_counts[underlying] = {"bullish": 0, "bearish": 0}
            side = alert.get("side", "")
            opt_type = alert.get("details", "").split()[0] if alert.get("details") else ""

            if (side == "BUY" and opt_type == "CALL") or (side == "SELL" and opt_type == "PUT"):
                alert_counts[underlying]["bullish"] += 1
            elif (side == "BUY" and opt_type == "PUT") or (side == "SELL" and opt_type == "CALL"):
                alert_counts[underlying]["bearish"] += 1

        for underlying, counts in alert_counts.items():
            bull = counts["bullish"]
            bear = counts["bearish"]
            total = bull + bear
            if total < 3:
                continue

            if bull > bear * 2:
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=strategy_id,
                    symbol=underlying,
                    direction=SignalDirection.LONG,
                    strength=min(bull / 10, 1.0),
                    timestamp=now,
                    metadata={"reason": "unusual_options_bullish", "bull": bull, "bear": bear},
                ))
            elif bear > bull * 2:
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=strategy_id,
                    symbol=underlying,
                    direction=SignalDirection.SHORT,
                    strength=min(bear / 10, 1.0),
                    timestamp=now,
                    metadata={"reason": "unusual_options_bearish", "bull": bull, "bear": bear},
                ))

        return signals

    def reset_daily(self) -> None:
        """Reset daily flow data (call at market close)."""
        self._daily_flow.clear()
        self._alerts.clear()
