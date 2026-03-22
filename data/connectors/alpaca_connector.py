"""Alpaca Markets data connector for real-time and historical market data.

Supports both paper and live trading via Alpaca's free API.
Provides SPY OHLCV, VIX data, and risk-free rate proxies.

Setup:
    1. Create free account at https://alpaca.markets
    2. Get API keys from dashboard (Paper Trading)
    3. Set environment variables:
        ALPACA_API_KEY=your_key
        ALPACA_SECRET_KEY=your_secret
        ALPACA_BASE_URL=https://paper-api.alpaca.markets  (paper)

Requires: pip install alpaca-py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional

import pandas as pd

from core.enums import AssetClass, TimeFrame
from core.logging import get_logger
from core.models import Bar
from data.connectors.base import DataConnector

logger = get_logger(__name__)


class AlpacaConnector(DataConnector):
    """Alpaca Markets connector for US equity data + paper trading.

    Provides:
      - Historical daily OHLCV for SPY, VIX proxy (via VIXY/UVXY or direct)
      - Real-time quotes during market hours
      - Paper trading order execution
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        paper: bool = True,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")

        if paper:
            self._base_url = base_url or os.environ.get(
                "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
            )
        else:
            self._base_url = base_url or "https://api.alpaca.markets"

        self._paper = paper
        self._client = None
        self._data_client = None
        self._trading_client = None
        self._connected = False
        self._subscriptions: set[str] = set()

    async def connect(self) -> None:
        """Connect to Alpaca API."""
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient

            self._data_client = StockHistoricalDataClient(
                api_key=self._api_key,
                secret_key=self._secret_key,
            )

            self._trading_client = TradingClient(
                api_key=self._api_key,
                secret_key=self._secret_key,
                paper=self._paper,
            )

            # Verify connection
            account = self._trading_client.get_account()
            self._connected = True
            logger.info(
                "alpaca_connected",
                paper=self._paper,
                equity=float(account.equity),
                buying_power=float(account.buying_power),
                status=account.status,
            )

        except ImportError:
            raise ImportError(
                "alpaca-py not installed. Run: pip install alpaca-py"
            )
        except Exception as e:
            logger.error("alpaca_connection_failed", error=str(e))
            raise

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("alpaca_disconnected")

    async def health_check(self) -> bool:
        if not self._connected or not self._trading_client:
            return False
        try:
            account = self._trading_client.get_account()
            return account.status == "ACTIVE"
        except Exception:
            return False

    async def subscribe(self, symbols: List[str], data_type: str = "bar") -> None:
        self._subscriptions.update(symbols)
        logger.info("alpaca_subscribed", symbols=symbols, data_type=data_type)

    async def unsubscribe(self, symbols: List[str]) -> None:
        self._subscriptions -= set(symbols)

    def get_historical_bars(
        self,
        symbol: str,
        start: datetime,
        end: Optional[datetime] = None,
        timeframe: str = "1Day",
    ) -> pd.DataFrame:
        """Fetch historical OHLCV bars.

        Args:
            symbol: Ticker symbol (e.g., "SPY")
            start: Start date
            end: End date (default: today)
            timeframe: "1Min", "5Min", "15Min", "1Hour", "1Day"

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame as AlpacaTF

        tf_map = {
            "1Min": AlpacaTF.Minute,
            "5Min": AlpacaTF(5, "Min"),
            "15Min": AlpacaTF(15, "Min"),
            "1Hour": AlpacaTF.Hour,
            "1Day": AlpacaTF.Day,
        }

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf_map.get(timeframe, AlpacaTF.Day),
            start=start,
            end=end or datetime.now(),
        )

        bars = self._data_client.get_stock_bars(request)
        records = []
        for bar in bars[symbol]:
            records.append({
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
                "vwap": float(bar.vwap) if bar.vwap else None,
            })
        return pd.DataFrame(records)

    def get_latest_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get the most recent bar for a symbol."""
        from alpaca.data.requests import StockLatestBarRequest

        request = StockLatestBarRequest(symbol_or_symbols=symbol)
        bars = self._data_client.get_stock_latest_bar(request)
        bar = bars.get(symbol)
        if bar:
            return {
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
        return None

    def get_latest_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get the latest quote (bid/ask) for a symbol."""
        from alpaca.data.requests import StockLatestQuoteRequest

        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = self._data_client.get_stock_latest_quote(request)
        quote = quotes.get(symbol)
        if quote:
            return {
                "timestamp": quote.timestamp,
                "bid": float(quote.bid_price),
                "ask": float(quote.ask_price),
                "bid_size": float(quote.bid_size),
                "ask_size": float(quote.ask_size),
            }
        return None

    def get_account(self) -> Dict[str, Any]:
        """Get paper trading account info."""
        account = self._trading_client.get_account()
        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "status": account.status,
            "pattern_day_trader": account.pattern_day_trader,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions from Alpaca."""
        positions = self._trading_client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": p.side,
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "market_value": float(p.market_value),
            }
            for p in positions
        ]

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Submit an order to Alpaca paper trading.

        Args:
            symbol: Ticker (e.g., "SPY")
            qty: Number of shares
            side: "buy" or "sell"
            order_type: "market", "limit", "stop"
            time_in_force: "day", "gtc", "ioc"
            limit_price: Required for limit orders

        Returns:
            Order confirmation dict
        """
        from alpaca.trading.requests import (
            MarketOrderRequest,
            LimitOrderRequest,
        )
        from alpaca.trading.enums import (
            OrderSide,
            TimeInForce,
        )

        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif_map = {
            "day": TimeInForce.DAY,
            "gtc": TimeInForce.GTC,
            "ioc": TimeInForce.IOC,
        }

        if order_type == "market":
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side_enum,
                time_in_force=tif_map.get(time_in_force, TimeInForce.DAY),
            )
        elif order_type == "limit":
            request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side_enum,
                time_in_force=tif_map.get(time_in_force, TimeInForce.DAY),
                limit_price=limit_price,
            )
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

        order = self._trading_client.submit_order(request)
        logger.info(
            "alpaca_order_submitted",
            symbol=symbol,
            side=side,
            qty=qty,
            order_id=str(order.id),
            status=order.status,
        )
        return {
            "order_id": str(order.id),
            "symbol": order.symbol,
            "side": str(order.side),
            "qty": float(order.qty),
            "status": str(order.status),
            "filled_qty": float(order.filled_qty) if order.filled_qty else 0,
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "submitted_at": str(order.submitted_at),
        }

    def get_orders(self, status: str = "open") -> List[Dict[str, Any]]:
        """Get orders from Alpaca."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        status_map = {
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all": QueryOrderStatus.ALL,
        }

        request = GetOrdersRequest(status=status_map.get(status, QueryOrderStatus.OPEN))
        orders = self._trading_client.get_orders(request)
        return [
            {
                "order_id": str(o.id),
                "symbol": o.symbol,
                "side": str(o.side),
                "qty": float(o.qty),
                "status": str(o.status),
                "filled_qty": float(o.filled_qty) if o.filled_qty else 0,
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
            }
            for o in orders
        ]

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """Stream real-time data (placeholder for WebSocket streaming)."""
        while self._connected:
            for symbol in self._subscriptions:
                bar = self.get_latest_bar(symbol)
                if bar:
                    yield {
                        "source": "alpaca",
                        "symbol": symbol,
                        "data_type": "bar",
                        "data": bar,
                    }
            await asyncio.sleep(60)  # Poll every 60s for daily strategy

    async def get_historical(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: datetime,
        end: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Bar]:
        """Get historical bars as Bar objects."""
        tf_map = {
            TimeFrame.M1: "1Min",
            TimeFrame.M5: "5Min",
            TimeFrame.M15: "15Min",
            TimeFrame.H1: "1Hour",
            TimeFrame.D1: "1Day",
        }

        df = self.get_historical_bars(
            symbol, start, end,
            timeframe=tf_map.get(timeframe, "1Day"),
        )

        bars = []
        for _, row in df.iterrows():
            bars.append(Bar(
                symbol=symbol,
                timestamp=row["timestamp"].to_pydatetime() if hasattr(row["timestamp"], "to_pydatetime") else row["timestamp"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                timeframe=timeframe,
                asset_class=AssetClass.EQUITY,
            ))
        return bars
