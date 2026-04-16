"""Market data provider for live trading via CCXT.

Fetches OHLCV bars from crypto exchanges for:
1. Historical warmup — load N bars to initialize strategy indicators
2. Live polling — detect new completed bars for signal generation
3. Ticker — current price for position monitoring

Uses the CCXT exchange instance from the execution engine
to avoid duplicate WebSocket/REST connections.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.logging import get_logger
from core.models import Bar

logger = get_logger(__name__)

# Timeframe durations in milliseconds
TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class CCXTBarProvider:
    """Polls CCXT exchange for new completed OHLCV bars.

    Designed for bar-based strategies (e.g., 4H BTC Sniper).
    Detects new bar closes by comparing timestamps.

    Usage:
        provider = CCXTBarProvider(exchange, "BTC/USDT:USDT", "4h")
        warmup_bars = await provider.fetch_warmup_bars(200)
        for bar in warmup_bars:
            strategy.on_bar(bar.symbol, bar)

        while running:
            bar = await provider.poll_new_bar()
            if bar:
                strategy.on_bar(bar.symbol, bar)
            await asyncio.sleep(30)
    """

    def __init__(
        self,
        exchange: Any,  # ccxt.pro exchange instance (already connected)
        symbol: str = "BTC/USDT:USDT",
        timeframe: str = "4h",
    ):
        self._exchange = exchange
        self._symbol = symbol
        self._timeframe = timeframe
        self._tf_ms = TF_MS.get(timeframe, 14_400_000)
        self._last_bar_ts: Optional[int] = None  # Last processed bar timestamp (ms)
        self._warmup_complete = False

    async def fetch_warmup_bars(self, count: int = 200) -> List[Bar]:
        """Fetch historical bars for indicator warmup.

        Fetches `count` completed bars, excluding the current incomplete bar.
        Bars are returned in chronological order (oldest first).

        Args:
            count: Number of historical bars to fetch (200 = ~33 days of 4H).

        Returns:
            List of Bar objects, oldest first.
        """
        logger.info(
            "fetching_warmup_bars",
            symbol=self._symbol,
            timeframe=self._timeframe,
            count=count,
        )

        all_candles: List[list] = []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        since = now_ms - (count + 10) * self._tf_ms  # Extra buffer

        while len(all_candles) < count:
            try:
                candles = await self._exchange.fetch_ohlcv(
                    self._symbol,
                    self._timeframe,
                    since=since,
                    limit=min(count - len(all_candles) + 5, 1000),
                )
                if not candles:
                    break

                all_candles.extend(candles)
                since = candles[-1][0] + 1  # Next batch after last candle

                if len(candles) < 100:
                    break  # No more data available

                await asyncio.sleep(0.3)  # Rate limit courtesy
            except Exception as e:
                logger.error("warmup_fetch_error", error=str(e))
                break

        # Remove the current (incomplete) bar — it hasn't closed yet
        current_bar_start = (now_ms // self._tf_ms) * self._tf_ms
        all_candles = [c for c in all_candles if c[0] < current_bar_start]

        # Deduplicate by timestamp
        seen: set = set()
        unique: List[list] = []
        for c in all_candles:
            if c[0] not in seen:
                seen.add(c[0])
                unique.append(c)

        # Sort chronologically and take last `count`
        unique.sort(key=lambda c: c[0])
        unique = unique[-count:]

        bars = [self._to_bar(c) for c in unique]

        if unique:
            self._last_bar_ts = unique[-1][0]

        self._warmup_complete = True
        logger.info(
            "warmup_complete",
            bars_loaded=len(bars),
            first=bars[0].timestamp.isoformat() if bars else "none",
            last=bars[-1].timestamp.isoformat() if bars else "none",
        )
        return bars

    async def poll_new_bar(self) -> Optional[Bar]:
        """Check if a new completed bar is available.

        Fetches the latest 3 candles. The second-to-last is the most recently
        COMPLETED bar (the last one is still forming). If its timestamp is
        newer than our last processed bar, return it.

        Returns:
            Bar if a new completed bar is available, None otherwise.
        """
        try:
            candles = await self._exchange.fetch_ohlcv(
                self._symbol,
                self._timeframe,
                limit=3,
            )
        except Exception as e:
            logger.warning("poll_fetch_error", error=str(e))
            return None

        if not candles or len(candles) < 2:
            return None

        # Last candle = current (incomplete), second-to-last = most recent completed
        completed = candles[-2]
        ts = completed[0]

        if self._last_bar_ts is not None and ts <= self._last_bar_ts:
            return None  # Already processed this bar

        self._last_bar_ts = ts
        bar = self._to_bar(completed)

        logger.info(
            "new_bar_detected",
            symbol=self._symbol,
            timeframe=self._timeframe,
            timestamp=bar.timestamp.isoformat(),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        return bar

    async def get_current_price(self) -> float:
        """Fetch current market price via ticker."""
        try:
            ticker = await self._exchange.fetch_ticker(self._symbol)
            return float(ticker.get("last", 0) or 0)
        except Exception:
            return 0.0

    def _to_bar(self, candle: list) -> Bar:
        """Convert CCXT OHLCV array [ts, o, h, l, c, v] to Bar model."""
        return Bar(
            symbol=self._symbol,
            timestamp=datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc),
            open=float(candle[1]),
            high=float(candle[2]),
            low=float(candle[3]),
            close=float(candle[4]),
            volume=float(candle[5] or 0),
        )

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def timeframe(self) -> str:
        return self._timeframe

    @property
    def is_warmed_up(self) -> bool:
        return self._warmup_complete
