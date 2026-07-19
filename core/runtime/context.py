"""MarketContext — a strategy's only window onto markets and its own book.

Two implementations, one interface:
  LiveContext      — real venue via ccxt (paper shadow / future live)
  BacktestContext  — catalog replay (core/runtime/backtest_host.py)

Strategies stay host-agnostic: same calls, same shapes, in both worlds.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import pandas as pd


class MarketContext(ABC):
    @abstractmethod
    def now(self) -> datetime: ...

    @abstractmethod
    def bars(self, symbol: str, tf: str, n: int) -> pd.DataFrame:
        """Last n CLOSED bars, columns timestamp/open/high/low/close/volume."""

    @abstractmethod
    def book(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """(best_bid, best_ask)."""

    @abstractmethod
    def funding(self, symbol: str) -> Optional[float]:
        """Current funding rate per interval, or None."""

    @abstractmethod
    def prev_bar_extremes(self, symbol: str) -> Tuple[float, float]:
        """(high, low) of the last closed primary-tf bar — maker fill checks."""

    # ── book state (populated by the host from the ledger) ──
    positions: Dict[str, dict]
    equity: float

    def position(self, symbol: str) -> Optional[dict]:
        return self.positions.get(symbol)


class LiveContext(MarketContext):
    """ccxt-backed context. One exchange handle shared per runner process."""

    def __init__(self, ex, symbol_fmt: str = "{base}/USDT:USDT",
                 primary_tf: str = "5m"):
        self.ex = ex
        self.symbol_fmt = symbol_fmt
        self.primary_tf = primary_tf
        self.positions = {}
        self.equity = 0.0
        self._bar_cache: Dict[tuple, tuple] = {}   # (sym, tf) -> (ts, df)

    def _sym(self, symbol: str) -> str:
        return (symbol if "/" in symbol
                else self.symbol_fmt.format(base=symbol))

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def bars(self, symbol: str, tf: str, n: int) -> pd.DataFrame:
        key = (symbol, tf)
        cached = self._bar_cache.get(key)
        if cached and time.time() - cached[0] < 30:
            return cached[1].tail(n)
        raw = self.ex.fetch_ohlcv(self._sym(symbol), tf, limit=min(n + 2, 300))
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low",
                                        "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.iloc[:-1]          # drop the still-forming bar
        self._bar_cache[key] = (time.time(), df)
        return df.tail(n)

    def book(self, symbol: str):
        ob = self.ex.fetch_order_book(self._sym(symbol), limit=5)
        bid = ob["bids"][0][0] if ob["bids"] else None
        ask = ob["asks"][0][0] if ob["asks"] else None
        return bid, ask

    def funding(self, symbol: str) -> Optional[float]:
        try:
            fr = self.ex.fetch_funding_rate(self._sym(symbol))
            v = fr.get("fundingRate")
            return float(v) if v is not None else None
        except Exception:
            return None

    def prev_bar_extremes(self, symbol: str):
        df = self.bars(symbol, self.primary_tf, 1)
        row = df.iloc[-1]
        return float(row["high"]), float(row["low"])


class BacktestContext(MarketContext):
    """Replay context. The host advances `cursor` bar by bar; strategies see
    only data up to the cursor. Book = synthetic from the current bar with a
    configurable half-spread. Funding replay optional via a series."""

    def __init__(self, data: Dict[str, pd.DataFrame], primary_tf: str,
                 half_spread_bps: float = 1.0,
                 funding_series: Optional[Dict[str, pd.Series]] = None):
        self.data = {s: df.reset_index(drop=True) for s, df in data.items()}
        self.primary_tf = primary_tf
        self.half_spread_bps = half_spread_bps
        self.funding_series = funding_series or {}
        self.cursor: int = 0                     # index of the CURRENT bar
        self.positions = {}
        self.equity = 0.0

    def now(self) -> datetime:
        s = next(iter(self.data))
        ts = self.data[s]["timestamp"].iloc[self.cursor]
        return ts.to_pydatetime().replace(tzinfo=timezone.utc)

    def bars(self, symbol: str, tf: str, n: int) -> pd.DataFrame:
        df = self.data[symbol]
        end = self.cursor                        # closed bars only: < cursor
        return df.iloc[max(0, end - n):end]

    def _px(self, symbol: str) -> float:
        return float(self.data[symbol]["open"].iloc[self.cursor])

    def book(self, symbol: str):
        # fills happen at the CURRENT bar's open ± half spread (no lookahead)
        px = self._px(symbol)
        h = px * self.half_spread_bps / 1e4
        return px - h, px + h

    def funding(self, symbol: str) -> Optional[float]:
        s = self.funding_series.get(symbol)
        if s is None:
            return None
        ts = self.data[symbol]["timestamp"].iloc[self.cursor]
        prior = s.loc[:ts]
        return float(prior.iloc[-1]) if len(prior) else None

    def prev_bar_extremes(self, symbol: str):
        i = max(self.cursor - 1, 0)
        row = self.data[symbol].iloc[i]
        return float(row["high"]), float(row["low"])

    def current_bar(self, symbol: str) -> pd.Series:
        return self.data[symbol].iloc[self.cursor]
