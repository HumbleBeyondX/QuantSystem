"""Live Paper Trading Runner for SPY Gamma Scalp Strategy.

Connects to real market data feeds and runs the gamma scalp strategy
in paper trading mode. Supports three data/execution backends:

  1. Yahoo Finance (simplest):  No account needed, delayed data, internal paper engine
  2. Alpaca (recommended):      Free paper trading API, real-time data + execution
  3. IB TWS (most realistic):   Full broker integration, real options/equity execution

Usage:
    # === Yahoo Finance (no account needed) ===
    python scripts/live_paper_trade.py --data-source yahoo --mode once
    python scripts/live_paper_trade.py --data-source yahoo --mode continuous --interval 60

    # === Alpaca Paper Trading (free, recommended) ===
    # First: set ALPACA_API_KEY and ALPACA_SECRET_KEY env vars
    python scripts/live_paper_trade.py --data-source alpaca --mode once
    python scripts/live_paper_trade.py --data-source alpaca --mode once --alpaca-execution

    # === IB TWS Paper Trading (most realistic) ===
    # First: start IB TWS/Gateway, enable API on port 7497
    python scripts/live_paper_trade.py --data-source ib --mode once
    python scripts/live_paper_trade.py --data-source ib --mode once --ib-execution
    python scripts/live_paper_trade.py --data-source ib --mode continuous --ib-execution --interval 60

    # === Testing with historical date ===
    python scripts/live_paper_trade.py --mode once --backfill-date 2026-03-18

Environment Variables:
    ALPACA_API_KEY      Alpaca API key (paper trading)
    ALPACA_SECRET_KEY   Alpaca secret key
    IB_HOST             IB TWS host (default: 127.0.0.1)
    IB_PORT             IB TWS port (default: 7497 for paper)
    IB_CLIENT_ID        IB client ID (default: 10)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import pickle
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env file if exists (for API keys)
_env_file = ROOT / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

from core.enums import AssetClass, TimeFrame
from core.models import Bar
from data.quality import DataQualityChecker, DataQualityError
from strategies.base import StrategyConfig
from strategies.registry import discover_strategies, create_strategy


# ===================================================================
# Constants
# ===================================================================

STATE_DIR = ROOT / "data" / "live_state"
STATE_FILE = STATE_DIR / "gamma_scalp_state.pkl"
LOG_DIR = ROOT / "logs" / "paper_trading"
TRADE_LOG = LOG_DIR / "gamma_scalp_trades.jsonl"
DAILY_LOG = LOG_DIR / "gamma_scalp_daily.jsonl"
CONFIG_PATH = ROOT / "config" / "strategies" / "spy_gamma_scalp_optimized.yaml"


# ===================================================================
# Data Fetchers
# ===================================================================

class YahooDataFetcher:
    """Fetch latest SPY + VIX + IRX data from Yahoo Finance."""

    def __init__(self):
        try:
            import yfinance as yf
            self._yf = yf
        except ImportError:
            raise ImportError("yfinance not installed. Run: pip install yfinance")

    def get_today_bar(self, date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch today's (or specified date's) bar for SPY + VIX + IRX.

        Returns dict with keys: timestamp, open, high, low, close, volume,
        vix_close, implied_vol, risk_free_rate
        """
        if date:
            end = pd.Timestamp(date) + timedelta(days=1)
            start = pd.Timestamp(date)
        else:
            end = datetime.now()
            start = end - timedelta(days=5)

        spy_data = self._yf.Ticker("SPY").history(start=start, end=end, interval="1d")
        vix_data = self._yf.Ticker("^VIX").history(start=start, end=end, interval="1d")
        irx_data = self._yf.Ticker("^IRX").history(start=start, end=end, interval="1d")

        if spy_data.empty:
            return None

        # Get latest row
        spy_row = spy_data.iloc[-1]
        ts = spy_data.index[-1]

        # Make timezone-naive
        if hasattr(ts, "tz") and ts.tz is not None:
            ts = ts.tz_localize(None)

        bar = {
            "timestamp": ts.to_pydatetime(),
            "open": float(spy_row["Open"]),
            "high": float(spy_row["High"]),
            "low": float(spy_row["Low"]),
            "close": float(spy_row["Close"]),
            "volume": float(spy_row["Volume"]),
        }

        # VIX
        if not vix_data.empty:
            # Find matching or closest date
            vix_ts = vix_data.index
            if hasattr(vix_ts[0], "tz") and vix_ts[0].tz is not None:
                vix_ts = vix_ts.tz_localize(None)
            closest_idx = vix_ts.get_indexer([ts], method="nearest")[0]
            if closest_idx >= 0:
                bar["vix_close"] = float(vix_data.iloc[closest_idx]["Close"])
                bar["implied_vol"] = bar["vix_close"] / 100.0

        # Risk-free rate
        if not irx_data.empty:
            irx_ts = irx_data.index
            if hasattr(irx_ts[0], "tz") and irx_ts[0].tz is not None:
                irx_ts = irx_ts.tz_localize(None)
            closest_idx = irx_ts.get_indexer([ts], method="nearest")[0]
            if closest_idx >= 0:
                bar["risk_free_rate"] = float(irx_data.iloc[closest_idx]["Close"]) / 100.0

        # Defaults
        bar.setdefault("vix_close", 20.0)
        bar.setdefault("implied_vol", 0.20)
        bar.setdefault("risk_free_rate", 0.045)

        return bar

    def get_historical_bars(self, days: int = 60) -> List[Dict[str, Any]]:
        """Fetch recent history for warm-up."""
        end = datetime.now()
        start = end - timedelta(days=int(days * 1.5))

        spy_data = self._yf.Ticker("SPY").history(start=start, end=end, interval="1d")
        vix_data = self._yf.Ticker("^VIX").history(start=start, end=end, interval="1d")
        irx_data = self._yf.Ticker("^IRX").history(start=start, end=end, interval="1d")

        if spy_data.empty:
            return []

        bars = []
        for ts, row in spy_data.iterrows():
            if hasattr(ts, "tz") and ts.tz is not None:
                ts = ts.tz_localize(None)

            bar = {
                "timestamp": ts.to_pydatetime(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }

            # VIX
            if not vix_data.empty:
                vix_ts = vix_data.index
                if hasattr(vix_ts[0], "tz") and vix_ts[0].tz is not None:
                    vix_ts = vix_ts.tz_localize(None)
                idx = vix_ts.get_indexer([ts], method="nearest")[0]
                if idx >= 0 and abs((vix_ts[idx] - ts).days) <= 3:
                    bar["vix_close"] = float(vix_data.iloc[idx]["Close"])
                    bar["implied_vol"] = bar["vix_close"] / 100.0

            # IRX
            if not irx_data.empty:
                irx_ts = irx_data.index
                if hasattr(irx_ts[0], "tz") and irx_ts[0].tz is not None:
                    irx_ts = irx_ts.tz_localize(None)
                idx = irx_ts.get_indexer([ts], method="nearest")[0]
                if idx >= 0 and abs((irx_ts[idx] - ts).days) <= 3:
                    bar["risk_free_rate"] = float(irx_data.iloc[idx]["Close"]) / 100.0

            bar.setdefault("vix_close", 20.0)
            bar.setdefault("implied_vol", 0.20)
            bar.setdefault("risk_free_rate", 0.045)
            bars.append(bar)

        return bars


class AlpacaDataFetcher:
    """Fetch data from Alpaca API + Yahoo for VIX."""

    def __init__(self):
        self._api_key = os.environ.get("ALPACA_API_KEY", "")
        self._secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

        if not self._api_key or not self._secret_key:
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.\n"
                "Get free keys at: https://alpaca.markets"
            )

        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._data_client = StockHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
        )
        self._trading_client = TradingClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=True,
        )

        # Yahoo for VIX (Alpaca doesn't have index data)
        self._yahoo = YahooDataFetcher()

    def get_account_info(self) -> Dict[str, Any]:
        """Get Alpaca paper trading account info."""
        account = self._trading_client.get_account()
        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "status": account.status,
        }

    def get_today_bar(self, date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch today's bar using Alpaca for SPY, Yahoo for VIX/IRX."""
        from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
        from alpaca.data.timeframe import TimeFrame as AlpacaTF

        if date:
            # Historical date
            start = pd.Timestamp(date)
            end = start + timedelta(days=1)
            request = StockBarsRequest(
                symbol_or_symbols="SPY",
                timeframe=AlpacaTF.Day,
                start=start.to_pydatetime(),
                end=end.to_pydatetime(),
            )
            bars = self._data_client.get_stock_bars(request)
            if "SPY" not in bars or len(bars["SPY"]) == 0:
                return self._yahoo.get_today_bar(date)
            bar_data = bars["SPY"][-1]
        else:
            # Latest bar
            request = StockLatestBarRequest(symbol_or_symbols="SPY")
            latest = self._data_client.get_stock_latest_bar(request)
            if "SPY" not in latest:
                return self._yahoo.get_today_bar()
            bar_data = latest["SPY"]

        ts = bar_data.timestamp
        if hasattr(ts, "tz") and ts.tz is not None:
            ts = ts.replace(tzinfo=None)

        bar = {
            "timestamp": ts,
            "open": float(bar_data.open),
            "high": float(bar_data.high),
            "low": float(bar_data.low),
            "close": float(bar_data.close),
            "volume": float(bar_data.volume),
        }

        # Get VIX + IRX from Yahoo (Alpaca doesn't have index data)
        yahoo_bar = self._yahoo.get_today_bar(date)
        if yahoo_bar:
            bar["vix_close"] = yahoo_bar.get("vix_close", 20.0)
            bar["implied_vol"] = yahoo_bar.get("implied_vol", 0.20)
            bar["risk_free_rate"] = yahoo_bar.get("risk_free_rate", 0.045)
        else:
            bar["vix_close"] = 20.0
            bar["implied_vol"] = 0.20
            bar["risk_free_rate"] = 0.045

        return bar

    def get_historical_bars(self, days: int = 60) -> List[Dict[str, Any]]:
        """Delegate to Yahoo for warm-up (simplicity)."""
        return self._yahoo.get_historical_bars(days)

    def submit_hedge_order(self, qty: int, side: str) -> Dict[str, Any]:
        """Submit delta hedge order to Alpaca paper trading."""
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if qty == 0:
            return {"status": "skipped", "reason": "qty=0"}

        request = MarketOrderRequest(
            symbol="SPY",
            qty=abs(qty),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self._trading_client.submit_order(request)
        return {
            "order_id": str(order.id),
            "symbol": "SPY",
            "side": side,
            "qty": abs(qty),
            "status": str(order.status),
        }


class IBDataFetcher:
    """Fetch data from IB TWS + Yahoo for VIX.

    IB TWS provides the most accurate real-time data for SPY,
    including real bid/ask spreads and option chain data.
    Uses Yahoo for VIX (IB doesn't provide VIX as easily).

    Setup:
        1. Download IB TWS or IB Gateway
        2. Login with paper trading credentials
        3. Enable API: Configure -> API -> Enable Socket Clients
        4. Default paper port: 7497
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 10,
    ):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = None
        self._connected = False
        self._yahoo = YahooDataFetcher()

        # Try to connect
        self._connect_ib()

    def _connect_ib(self) -> None:
        """Connect to IB TWS."""
        try:
            from ib_insync import IB, Stock
            self._ib = IB()
            self._ib.connect(
                self._host, self._port,
                clientId=self._client_id,
                timeout=15,
            )
            self._connected = True

            # Print account info
            accounts = self._ib.managedAccounts()
            print(f"  [IB] Connected to TWS at {self._host}:{self._port}")
            print(f"  [IB] Accounts: {accounts}")
            print(f"  [IB] Paper trading: {'Yes' if self._port == 7497 else 'No (LIVE!)'}")

        except ImportError:
            raise ImportError("ib_insync not installed. Run: pip install ib_insync")
        except Exception as e:
            raise ConnectionError(
                f"Cannot connect to IB TWS at {self._host}:{self._port}.\n"
                f"  Error: {e}\n\n"
                f"  Setup instructions:\n"
                f"  1. Download IB TWS: https://www.interactivebrokers.com/en/trading/tws.php\n"
                f"  2. Login with paper trading credentials\n"
                f"  3. Enable API: Configure -> API -> Settings\n"
                f"     - Check 'Enable ActiveX and Socket Clients'\n"
                f"     - Socket port: 7497 (paper) or 7496 (live)\n"
                f"     - Uncheck 'Read-Only API'\n"
                f"  4. Add 127.0.0.1 to trusted IPs\n"
            )

    def get_today_bar(self, date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch today's bar from IB for SPY, Yahoo for VIX/IRX."""
        from ib_insync import Stock

        if not self._connected:
            return self._yahoo.get_today_bar(date)

        contract = Stock("SPY", "SMART", "USD")
        self._ib.qualifyContracts(contract)

        if date:
            # Historical date
            bars = self._ib.reqHistoricalData(
                contract,
                endDateTime=date.replace("-", "") + " 23:59:59",
                durationStr="1 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            )
        else:
            # Latest bar
            bars = self._ib.reqHistoricalData(
                contract,
                endDateTime="",  # Latest
                durationStr="1 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            )

        if not bars:
            return self._yahoo.get_today_bar(date)

        ib_bar = bars[-1]
        ts = ib_bar.date
        if isinstance(ts, str):
            ts = datetime.strptime(ts, "%Y-%m-%d")
        elif not isinstance(ts, datetime):
            ts = datetime.combine(ts, datetime.min.time())

        bar = {
            "timestamp": ts,
            "open": float(ib_bar.open),
            "high": float(ib_bar.high),
            "low": float(ib_bar.low),
            "close": float(ib_bar.close),
            "volume": float(ib_bar.volume),
        }

        # Get VIX + IRX from Yahoo
        yahoo_bar = self._yahoo.get_today_bar(date)
        if yahoo_bar:
            bar["vix_close"] = yahoo_bar.get("vix_close", 20.0)
            bar["implied_vol"] = yahoo_bar.get("implied_vol", 0.20)
            bar["risk_free_rate"] = yahoo_bar.get("risk_free_rate", 0.045)
        else:
            bar["vix_close"] = 20.0
            bar["implied_vol"] = 0.20
            bar["risk_free_rate"] = 0.045

        return bar

    def get_historical_bars(self, days: int = 60) -> List[Dict[str, Any]]:
        """Fetch historical bars from IB, with Yahoo fallback."""
        if not self._connected:
            return self._yahoo.get_historical_bars(days)

        from ib_insync import Stock

        contract = Stock("SPY", "SMART", "USD")
        self._ib.qualifyContracts(contract)

        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=f"{days} D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
        )

        if not bars:
            return self._yahoo.get_historical_bars(days)

        # Get VIX history from Yahoo
        yahoo_bars = self._yahoo.get_historical_bars(days)
        yahoo_by_date = {}
        for yb in yahoo_bars:
            yahoo_by_date[yb["timestamp"].date()] = yb

        result = []
        for ib_bar in bars:
            ts = ib_bar.date
            if isinstance(ts, str):
                ts = datetime.strptime(ts, "%Y-%m-%d")
            elif not isinstance(ts, datetime):
                ts = datetime.combine(ts, datetime.min.time())

            bar = {
                "timestamp": ts,
                "open": float(ib_bar.open),
                "high": float(ib_bar.high),
                "low": float(ib_bar.low),
                "close": float(ib_bar.close),
                "volume": float(ib_bar.volume),
            }

            # Merge VIX/IRX from Yahoo
            yb = yahoo_by_date.get(ts.date(), {})
            bar["vix_close"] = yb.get("vix_close", 20.0)
            bar["implied_vol"] = yb.get("implied_vol", 0.20)
            bar["risk_free_rate"] = yb.get("risk_free_rate", 0.045)

            result.append(bar)

        return result

    def get_account_info(self) -> Dict[str, Any]:
        """Get IB account info."""
        if not self._connected:
            return {"error": "not connected"}

        summary = self._ib.accountSummary()
        result = {}
        for item in summary:
            result[item.tag] = item.value

        return {
            "equity": float(result.get("NetLiquidation", 0)),
            "cash": float(result.get("TotalCashValue", 0)),
            "buying_power": float(result.get("BuyingPower", 0)),
            "unrealized_pnl": float(result.get("UnrealizedPnL", 0)),
        }

    def submit_hedge_order(self, qty: int, side: str) -> Dict[str, Any]:
        """Submit delta hedge order via IB."""
        if not self._connected:
            return {"status": "error", "reason": "not connected"}
        if qty == 0:
            return {"status": "skipped", "reason": "qty=0"}

        from ib_insync import Stock, MarketOrder

        contract = Stock("SPY", "SMART", "USD")
        self._ib.qualifyContracts(contract)

        action = "BUY" if side == "buy" else "SELL"
        order = MarketOrder(action, abs(qty))
        trade = self._ib.placeOrder(contract, order)

        # Wait for fill
        self._ib.sleep(1.0)

        fill_price = None
        if trade.fills:
            fill_price = trade.fills[-1].execution.price

        return {
            "order_id": str(trade.order.orderId),
            "symbol": "SPY",
            "side": side,
            "qty": abs(qty),
            "status": str(trade.orderStatus.status),
            "fill_price": fill_price,
        }

    def submit_straddle_order(
        self,
        strike: float,
        expiry: str,
        action: str,
        quantity: int,
    ) -> Dict[str, Any]:
        """Submit straddle order (call + put) via IB.

        Args:
            strike: Strike price
            expiry: Expiration date (YYYYMMDD)
            action: "BUY" or "SELL"
            quantity: Number of contracts

        Returns:
            Dict with call and put order details
        """
        if not self._connected:
            return {"status": "error", "reason": "not connected"}

        from ib_insync import Option, MarketOrder

        # Create call contract
        call = Option("SPY", expiry, strike, "C", "SMART")
        put = Option("SPY", expiry, strike, "P", "SMART")
        self._ib.qualifyContracts(call, put)

        call_order = MarketOrder(action, quantity)
        put_order = MarketOrder(action, quantity)

        call_trade = self._ib.placeOrder(call, call_order)
        put_trade = self._ib.placeOrder(put, put_order)

        # Wait for fills
        self._ib.sleep(2.0)

        return {
            "call_order_id": str(call_trade.order.orderId),
            "put_order_id": str(put_trade.order.orderId),
            "call_status": str(call_trade.orderStatus.status),
            "put_status": str(put_trade.orderStatus.status),
            "strike": strike,
            "expiry": expiry,
            "action": action,
            "quantity": quantity,
        }

    def get_spy_option_greeks(self, strike: float, expiry: str) -> Dict[str, Any]:
        """Get real option Greeks from IB for a SPY straddle."""
        if not self._connected:
            return {}

        from ib_insync import Option

        call = Option("SPY", expiry, strike, "C", "SMART")
        put = Option("SPY", expiry, strike, "P", "SMART")
        self._ib.qualifyContracts(call, put)

        # Request market data
        call_ticker = self._ib.reqMktData(call, genericTickList="106")
        put_ticker = self._ib.reqMktData(put, genericTickList="106")
        self._ib.sleep(3.0)

        result = {}
        for name, ticker in [("call", call_ticker), ("put", put_ticker)]:
            greeks = ticker.modelGreeks or ticker.lastGreeks
            if greeks:
                result[name] = {
                    "delta": greeks.delta or 0,
                    "gamma": greeks.gamma or 0,
                    "theta": greeks.theta or 0,
                    "vega": greeks.vega or 0,
                    "implied_vol": greeks.impliedVol or 0,
                    "price": ticker.last if ticker.last > 0 else ticker.close,
                }

        self._ib.cancelMktData(call)
        self._ib.cancelMktData(put)

        return result

    def disconnect(self) -> None:
        """Disconnect from IB."""
        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False


# ===================================================================
# State Management
# ===================================================================

class StrategyStateManager:
    """Persist and restore strategy state between runs."""

    @staticmethod
    def save_state(strategy, filepath: Path = STATE_FILE) -> None:
        """Save strategy's internal state to disk."""
        filepath.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "timestamp": datetime.now().isoformat(),
            # Bar history
            "_bars": {k: list(v) for k, v in strategy._bars.items()},
            "_current_time": strategy._current_time,
            "_prev_time": strategy._prev_time,
            "_last_spot": strategy._last_spot,
            "_implied_vol": strategy._implied_vol,
            "_risk_free": strategy._risk_free,
            # Feature history
            "_vrp_history": list(strategy._vrp_history),
            "_range_history": list(strategy._range_history),
            "_gap_history": list(strategy._gap_history),
            # Realized vol
            "_rv_5d": strategy._rv_5d,
            "_rv_10d": strategy._rv_10d,
            "_rv_20d": strategy._rv_20d,
            # Position
            "_straddle_open": strategy._straddle_open,
            "_trade_direction": strategy._trade_direction,
            "_strike": strategy._strike,
            "_entry_call_price": strategy._entry_call_price,
            "_entry_put_price": strategy._entry_put_price,
            "_straddle_entry_cost": strategy._straddle_entry_cost,
            "_entry_spot": strategy._entry_spot,
            "_expiry_time": strategy._expiry_time,
            "_entry_iv": strategy._entry_iv,
            # Greeks
            "_portfolio_delta": strategy._portfolio_delta,
            "_portfolio_gamma": strategy._portfolio_gamma,
            "_portfolio_theta": strategy._portfolio_theta,
            "_portfolio_vega": strategy._portfolio_vega,
            # Hedge
            "_hedge_shares": strategy._hedge_shares,
            "_last_hedge_price": strategy._last_hedge_price,
            # P&L
            "_cum_hedge_pnl": strategy._cum_hedge_pnl,
            "_cum_gamma_pnl": strategy._cum_gamma_pnl,
            "_cum_theta_pnl": strategy._cum_theta_pnl,
            "_cum_vega_pnl": strategy._cum_vega_pnl,
            "_cum_txn_costs": strategy._cum_txn_costs,
            "_prev_iv": strategy._prev_iv,
            # Risk
            "_consecutive_losses": strategy._consecutive_losses,
            "_cooldown_until": strategy._cooldown_until,
            "_peak_equity": strategy._peak_equity,
            "_running_pnl": strategy._running_pnl,
            "_n_contracts": strategy._n_contracts,
            "_trade_peak_pnl": strategy._trade_peak_pnl,
            "_initial_capital": strategy._initial_capital,
            # Trade history
            "_trade_results": strategy._trade_results,
        }

        with open(filepath, "wb") as f:
            pickle.dump(state, f)

    @staticmethod
    def load_state(strategy, filepath: Path = STATE_FILE) -> bool:
        """Restore strategy state from disk. Returns True if state was loaded."""
        if not filepath.exists():
            return False

        try:
            with open(filepath, "rb") as f:
                state = pickle.load(f)

            # Restore bar history as deques
            from collections import deque
            for k, v in state.get("_bars", {}).items():
                strategy._bars[k] = deque(v, maxlen=300)

            # Restore feature histories as deques
            strategy._vrp_history = deque(state.get("_vrp_history", []), maxlen=120)
            strategy._range_history = deque(state.get("_range_history", []), maxlen=60)
            strategy._gap_history = deque(state.get("_gap_history", []), maxlen=60)

            # Restore scalars
            for key in [
                "_current_time", "_prev_time", "_last_spot", "_implied_vol",
                "_risk_free", "_rv_5d", "_rv_10d", "_rv_20d",
                "_straddle_open", "_trade_direction", "_strike",
                "_entry_call_price", "_entry_put_price", "_straddle_entry_cost",
                "_entry_spot", "_expiry_time", "_entry_iv",
                "_portfolio_delta", "_portfolio_gamma", "_portfolio_theta",
                "_portfolio_vega", "_hedge_shares", "_last_hedge_price",
                "_cum_hedge_pnl", "_cum_gamma_pnl", "_cum_theta_pnl",
                "_cum_vega_pnl", "_cum_txn_costs", "_prev_iv",
                "_consecutive_losses", "_cooldown_until", "_peak_equity",
                "_running_pnl", "_n_contracts", "_trade_peak_pnl",
                "_initial_capital", "_trade_results",
            ]:
                if key in state:
                    setattr(strategy, key, state[key])

            return True
        except Exception as e:
            print(f"  WARNING: Failed to load state: {e}")
            return False


# ===================================================================
# Signal Logger
# ===================================================================

class SignalLogger:
    """Log signals, trades, and daily snapshots to JSONL files."""

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def log_daily(self, data: Dict[str, Any]) -> None:
        """Append daily snapshot to JSONL."""
        data["logged_at"] = datetime.now().isoformat()
        with open(DAILY_LOG, "a") as f:
            f.write(json.dumps(data, default=str) + "\n")

    def log_trade(self, data: Dict[str, Any]) -> None:
        """Append trade event to JSONL."""
        data["logged_at"] = datetime.now().isoformat()
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(data, default=str) + "\n")


# ===================================================================
# Main Paper Trading Engine
# ===================================================================

class PaperTradingRunner:
    """Orchestrates live paper trading for the gamma scalp strategy."""

    def __init__(
        self,
        data_source: str = "yahoo",
        initial_capital: float = 100_000,
        use_alpaca_execution: bool = False,
        use_ib_execution: bool = False,
        ib_host: str = "127.0.0.1",
        ib_port: int = 7497,
        ib_client_id: int = 10,
    ):
        self.data_source = data_source
        self.initial_capital = initial_capital
        self.use_alpaca_execution = use_alpaca_execution
        self.use_ib_execution = use_ib_execution
        self.logger = SignalLogger()
        self.dqc = DataQualityChecker()

        # Initialize data fetcher
        if data_source == "ib":
            self.fetcher = IBDataFetcher(
                host=ib_host,
                port=ib_port,
                client_id=ib_client_id,
            )
        elif data_source == "alpaca":
            self.fetcher = AlpacaDataFetcher()
        else:
            self.fetcher = YahooDataFetcher()

        # Initialize strategy
        discover_strategies()
        self.strategy = self._create_strategy()
        self.state_manager = StrategyStateManager()

        # Try to restore previous state
        if self.state_manager.load_state(self.strategy):
            print("  [OK] Restored strategy state from previous session")
            print(f"       Running P&L: ${self.strategy._running_pnl:,.2f}")
            print(f"       Trades completed: {len(self.strategy._trade_results)}")
            if self.strategy._straddle_open:
                print(f"       Active position: {self.strategy._trade_direction}")
                print(f"       Strike: {self.strategy._strike}")
                print(f"       Contracts: {self.strategy._n_contracts}")
        else:
            print("  [INFO] No previous state found. Starting fresh.")
            self._warm_up_strategy()

    def _create_strategy(self):
        """Create strategy instance with optimized parameters."""
        import yaml

        # Load optimized params from YAML
        params = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f)
                params = cfg.get("parameters", {})

        params["initial_capital"] = self.initial_capital

        config = StrategyConfig(
            strategy_id="gamma_scalp_live",
            name="Gamma Scalp Live Paper",
            asset_class=AssetClass.EQUITY,
            symbols=["SPY"],
            parameters=params,
            mode="paper",
        )
        return create_strategy("spy_gamma_scalp", config)

    def _warm_up_strategy(self) -> None:
        """Feed historical bars to build up internal state (RV, signal history)."""
        print("  [WARMUP] Fetching 60 days of history for strategy warm-up...")

        try:
            bars = self.fetcher.get_historical_bars(days=60)
        except Exception as e:
            print(f"  WARNING: Warm-up failed: {e}")
            return

        if not bars:
            print("  WARNING: No historical data for warm-up")
            return

        # ── Data Quality Check on historical bars ──
        try:
            bars_report = self.dqc.check_bars(bars)
            bars = bars_report.bar_data  # Use cleaned bars
            print(f"  [DQC] Historical data: {bars_report.summary}")
        except DataQualityError as e:
            print(f"  [DQC] CRITICAL: Historical data failed quality check: {e}")
            print("  [DQC] Aborting warm-up. Strategy will not trade until data is fixed.")
            return

        self.strategy.initialize()

        # Feed all bars EXCEPT the last one for warm-up only (indicators/RV/z-scores)
        # Reserve the last bar so process_bar() can process it as a "new" bar
        warmup_bars = bars[:-1] if len(bars) > 1 else bars
        for bar_data in warmup_bars:
            bar = Bar(
                symbol="SPY",
                timestamp=bar_data["timestamp"],
                open=bar_data["open"],
                high=bar_data["high"],
                low=bar_data["low"],
                close=bar_data["close"],
                volume=bar_data["volume"],
                timeframe=TimeFrame.D1,
                asset_class=AssetClass.EQUITY,
                metadata={
                    "implied_vol": bar_data.get("implied_vol", 0.20),
                    "risk_free_rate": bar_data.get("risk_free_rate", 0.045),
                    "vix_close": bar_data.get("vix_close", 20.0),
                },
            )
            self.strategy.on_bar("SPY", bar)
            self.strategy.calculate_target_positions()

        print(f"  [WARMUP] Fed {len(warmup_bars)} bars. RV(20d): {self.strategy._rv_20d:.4f}")
        print(f"  [WARMUP] Last warmup bar: {warmup_bars[-1]['timestamp']}")
        print(f"  [WARMUP] Next bar to process: {bars[-1]['timestamp']}")

    def process_bar(self, bar_data: Dict[str, Any]) -> Dict[str, Any]:
        """Feed one bar to the strategy and process signals.

        Returns a summary dict of what happened.
        """
        ts = bar_data["timestamp"]
        spot = bar_data["close"]

        # Check if we already processed this bar
        if (self.strategy._current_time and
            hasattr(self.strategy._current_time, "date") and
            ts.date() == self.strategy._current_time.date()):
            return {"status": "skipped", "reason": "already_processed", "date": str(ts.date())}

        # Track pre-state
        pre_open = self.strategy._straddle_open
        pre_pnl = self.strategy._running_pnl
        pre_trades = len(self.strategy._trade_results)
        pre_hedge_shares = self.strategy._hedge_shares if self.strategy._straddle_open else 0

        # Feed bar
        bar = Bar(
            symbol="SPY",
            timestamp=ts,
            open=bar_data["open"],
            high=bar_data["high"],
            low=bar_data["low"],
            close=spot,
            volume=bar_data["volume"],
            timeframe=TimeFrame.D1,
            asset_class=AssetClass.EQUITY,
            metadata={
                "implied_vol": bar_data.get("implied_vol", 0.20),
                "risk_free_rate": bar_data.get("risk_free_rate", 0.045),
                "vix_close": bar_data.get("vix_close", 20.0),
            },
        )
        self.strategy.on_bar("SPY", bar)
        targets = self.strategy.calculate_target_positions()

        # Determine what happened
        post_open = self.strategy._straddle_open
        post_pnl = self.strategy._running_pnl
        post_trades = len(self.strategy._trade_results)
        post_hedge_shares = self.strategy._hedge_shares if self.strategy._straddle_open else 0

        actions = []
        if not pre_open and post_open:
            actions.append("OPENED_POSITION")
        elif pre_open and not post_open:
            actions.append("CLOSED_POSITION")
        elif pre_open and post_open:
            if abs(post_hedge_shares - pre_hedge_shares) >= 1:
                actions.append("DELTA_HEDGE")
        if not actions:
            actions.append("NO_ACTION")

        # Execute hedge orders via broker if configured
        hedge_delta = post_hedge_shares - pre_hedge_shares
        broker_order = None
        if abs(hedge_delta) >= 1:
            side = "buy" if hedge_delta > 0 else "sell"
            qty = abs(int(hedge_delta))

            if self.use_ib_execution and hasattr(self.fetcher, "submit_hedge_order"):
                try:
                    broker_order = self.fetcher.submit_hedge_order(qty=qty, side=side)
                    actions.append(f"IB_HEDGE_{side.upper()}_{qty}shr")
                    if broker_order.get("fill_price"):
                        actions.append(f"@${broker_order['fill_price']:.2f}")
                except Exception as e:
                    actions.append(f"IB_HEDGE_FAILED: {e}")

            elif self.use_alpaca_execution and hasattr(self.fetcher, "submit_hedge_order"):
                try:
                    broker_order = self.fetcher.submit_hedge_order(qty=qty, side=side)
                    actions.append(f"ALPACA_HEDGE_{side.upper()}_{qty}shr")
                except Exception as e:
                    actions.append(f"ALPACA_HEDGE_FAILED: {e}")

        # Compute unrealized P&L for open position
        from strategies.options.gamma_scalp_strategy import bs_straddle
        unrealized = 0.0
        if self.strategy._straddle_open and self.strategy._n_contracts > 0:
            T = self.strategy._time_to_expiry()
            iv = self.strategy._implied_vol
            rf = self.strategy._risk_free
            cp, pp = bs_straddle(spot, self.strategy._strike, T, rf, iv)
            n = self.strategy._n_contracts * self.strategy.multiplier
            sign = -1.0 if self.strategy._trade_direction == "sell_vol" else 1.0
            unrealized = ((cp + pp) - (self.strategy._entry_call_price + self.strategy._entry_put_price)) * n * sign
            unrealized += self.strategy._cum_hedge_pnl - self.strategy._cum_txn_costs

        equity = self.initial_capital + self.strategy._running_pnl + unrealized

        summary = {
            "date": str(ts.date()),
            "timestamp": ts.isoformat(),
            "spy_close": round(spot, 2),
            "vix": round(bar_data.get("vix_close", 0), 2),
            "implied_vol": round(bar_data.get("implied_vol", 0), 4),
            "rv_20d": round(self.strategy._rv_20d, 4),
            "actions": actions,
            "position_open": post_open,
            "trade_direction": self.strategy._trade_direction if post_open else "none",
            "contracts": self.strategy._n_contracts if post_open else 0,
            "strike": self.strategy._strike if post_open else 0,
            "hedge_shares": round(post_hedge_shares, 0) if post_open else 0,
            "realized_pnl": round(post_pnl, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_equity": round(equity, 2),
            "total_trades": post_trades,
            "consecutive_losses": self.strategy._consecutive_losses,
            "portfolio_delta": round(self.strategy._portfolio_delta, 2) if post_open else 0,
            "portfolio_gamma": round(self.strategy._portfolio_gamma, 2) if post_open else 0,
            "portfolio_theta": round(self.strategy._portfolio_theta, 2) if post_open else 0,
        }

        # Log new trades
        if post_trades > pre_trades:
            for trade in self.strategy._trade_results[pre_trades:]:
                self.logger.log_trade(trade)
                summary["last_trade"] = trade

        if broker_order:
            summary["broker_order"] = broker_order

        # Log daily
        self.logger.log_daily(summary)

        # Save state
        self.state_manager.save_state(self.strategy)

        return summary

    def run_once(self, date: Optional[str] = None) -> Dict[str, Any]:
        """Process today's bar (or specified date) and return summary."""
        bar_data = self.fetcher.get_today_bar(date)
        if bar_data is None:
            return {"status": "no_data", "reason": "market_closed_or_no_data"}

        # ── Data Quality Check ──
        try:
            report = self.dqc.check_bar(bar_data)
            bar_data = report.bar_data  # Use cleaned data
            if report.has_warnings:
                print(f"  [DQC] {report.summary}")
        except DataQualityError as e:
            error_msg = str(e)
            print(f"  [DQC] CRITICAL: {error_msg}")
            print("  [DQC] Skipping this bar. Strategy state preserved.")
            # Send email alert for data quality failure
            self._send_dq_alert(error_msg, date)
            return {"status": "data_quality_error", "reason": error_msg, "date": date}

        return self.process_bar(bar_data)

    def _send_dq_alert(self, error_msg: str, date: Optional[str] = None) -> None:
        """Send email alert on data quality failure."""
        try:
            import asyncio
            from dashboard.alerts.email_notifier import get_email_notifier
            from dashboard.alerts.alert_manager import Alert, AlertSeverity

            notifier = get_email_notifier()
            if not notifier.is_configured:
                return

            alert = Alert(
                title="Data Quality Check Failed",
                message=f"Paper trading run skipped due to data quality issue.\n\n{error_msg}",
                severity=AlertSeverity.CRITICAL,
                source="paper_trading_dqc",
                metadata={
                    "date": date or "today",
                    "data_source": self.data_source,
                    "strategy": "spy_gamma_scalp",
                },
            )
            asyncio.run(notifier.send_alert(alert))
            print("  [DQC] Alert email sent.")
        except Exception as e:
            print(f"  [DQC] Failed to send alert email: {e}")

    def run_continuous(self, interval_minutes: int = 60) -> None:
        """Run in a loop, checking for new data periodically."""
        print(f"\n  Starting continuous paper trading (interval={interval_minutes}min)")
        print("  Press Ctrl+C to stop\n")

        running = True

        def handle_signal(sig, frame):
            nonlocal running
            print("\n  Shutting down gracefully...")
            running = False

        signal.signal(signal.SIGINT, handle_signal)

        last_processed_date = None

        while running:
            try:
                bar_data = self.fetcher.get_today_bar()
                if bar_data:
                    bar_date = bar_data["timestamp"].date()
                    if bar_date != last_processed_date:
                        summary = self.process_bar(bar_data)
                        self._print_summary(summary)
                        last_processed_date = bar_date
                    else:
                        print(f"  [{datetime.now().strftime('%H:%M:%S')}] Already processed {bar_date}. Waiting...")
                else:
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] No new data. Market may be closed.")

            except Exception as e:
                print(f"  ERROR: {e}")

            if running:
                time.sleep(interval_minutes * 60)

    def _print_summary(self, summary: Dict[str, Any]) -> None:
        """Pretty-print the daily summary."""
        print(f"\n  {'='*60}")
        print(f"  DAILY PAPER TRADING REPORT - {summary.get('date', 'N/A')}")
        print(f"  {'='*60}")
        print(f"  SPY Close:       ${summary.get('spy_close', 0):>10.2f}")
        print(f"  VIX:             {summary.get('vix', 0):>10.2f}")
        print(f"  IV:              {summary.get('implied_vol', 0):>10.4f}")
        print(f"  RV(20d):         {summary.get('rv_20d', 0):>10.4f}")
        print(f"  VRP (IV-RV):     {summary.get('implied_vol', 0) - summary.get('rv_20d', 0):>+10.4f}")
        print()
        print(f"  Actions:         {', '.join(summary.get('actions', []))}")
        if summary.get("position_open"):
            print(f"  Direction:       {summary.get('trade_direction', 'N/A')}")
            print(f"  Contracts:       {summary.get('contracts', 0)}")
            print(f"  Strike:          ${summary.get('strike', 0):.0f}")
            print(f"  Hedge Shares:    {summary.get('hedge_shares', 0):.0f}")
            print(f"  Delta:           {summary.get('portfolio_delta', 0):>+10.1f}")
            print(f"  Gamma:           {summary.get('portfolio_gamma', 0):>10.1f}")
            print(f"  Theta:           {summary.get('portfolio_theta', 0):>+10.1f}")
        print()
        print(f"  Realized P&L:    ${summary.get('realized_pnl', 0):>+10,.2f}")
        print(f"  Unrealized P&L:  ${summary.get('unrealized_pnl', 0):>+10,.2f}")
        print(f"  Total Equity:    ${summary.get('total_equity', 0):>10,.2f}")
        print(f"  Total Trades:    {summary.get('total_trades', 0)}")
        print(f"  Consec Losses:   {summary.get('consecutive_losses', 0)}")

        if "last_trade" in summary:
            t = summary["last_trade"]
            print(f"\n  >> TRADE CLOSED: {t.get('direction', '')} | "
                  f"PnL: ${t.get('total_pnl', 0):+,.2f} | "
                  f"Reason: {t.get('reason', '')}")

        if "broker_order" in summary:
            o = summary["broker_order"]
            broker = "IB" if self.data_source == "ib" else "ALPACA"
            fill_info = f" @ ${o['fill_price']:.2f}" if o.get('fill_price') else ""
            print(f"  >> {broker} ORDER: {o.get('side', '')} {o.get('qty', 0)} SPY{fill_info} | "
                  f"Status: {o.get('status', '')}")

        print(f"  {'='*60}\n")


# ===================================================================
# CLI Entry Point
# ===================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Live Paper Trading - SPY Gamma Scalp Strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test with today's data (Yahoo Finance, no account needed)
  python scripts/live_paper_trade.py --mode once

  # Test with a specific date
  python scripts/live_paper_trade.py --mode once --backfill-date 2026-03-18

  # Run continuously, checking every 60 minutes
  python scripts/live_paper_trade.py --mode continuous --interval 60

  # Use Alpaca for data + paper execution
  python scripts/live_paper_trade.py --data-source alpaca --mode once --alpaca-execution

  # Reset state and start fresh
  python scripts/live_paper_trade.py --mode once --reset-state
""",
    )

    parser.add_argument(
        "--mode", choices=["once", "continuous"],
        default="once",
        help="Run mode: 'once' processes one bar, 'continuous' runs in a loop",
    )
    parser.add_argument(
        "--data-source", choices=["yahoo", "alpaca", "ib"],
        default="yahoo",
        help="Data source: 'yahoo' (free), 'alpaca' (free API), 'ib' (IB TWS)",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Check interval in minutes (continuous mode only)",
    )
    parser.add_argument(
        "--initial-capital", type=float, default=100_000,
        help="Initial paper trading capital",
    )
    parser.add_argument(
        "--backfill-date", type=str, default=None,
        help="Process a specific historical date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--alpaca-execution", action="store_true",
        help="Submit delta hedge orders to Alpaca paper trading",
    )
    parser.add_argument(
        "--ib-execution", action="store_true",
        help="Submit hedge orders to IB TWS paper trading",
    )
    parser.add_argument(
        "--ib-host", type=str, default="127.0.0.1",
        help="IB TWS host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--ib-port", type=int, default=7497,
        help="IB TWS port (7497=paper, 7496=live)",
    )
    parser.add_argument(
        "--ib-client-id", type=int, default=10,
        help="IB TWS client ID",
    )
    parser.add_argument(
        "--reset-state", action="store_true",
        help="Clear saved state and start fresh",
    )

    args = parser.parse_args()

    # Determine execution mode
    exec_mode = "internal"
    if args.ib_execution or args.data_source == "ib":
        exec_mode = "IB TWS"
    elif args.alpaca_execution:
        exec_mode = "Alpaca"

    print("=" * 60)
    print("  SPY GAMMA SCALP - LIVE PAPER TRADING")
    print("=" * 60)
    print(f"  Mode:            {args.mode}")
    print(f"  Data Source:     {args.data_source}")
    print(f"  Execution:       {exec_mode}")
    print(f"  Capital:         ${args.initial_capital:,.0f}")
    if args.data_source == "ib":
        print(f"  IB Host:         {args.ib_host}:{args.ib_port}")
    print(f"  State File:      {STATE_FILE}")
    print()

    # Reset state if requested
    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("  [RESET] Cleared saved state")

    # Create runner
    try:
        runner = PaperTradingRunner(
            data_source=args.data_source,
            initial_capital=args.initial_capital,
            use_alpaca_execution=args.alpaca_execution,
            use_ib_execution=args.ib_execution,
            ib_host=args.ib_host,
            ib_port=args.ib_port,
            ib_client_id=args.ib_client_id,
        )
    except Exception as e:
        print(f"\n  ERROR: Failed to initialize: {e}")
        if "ALPACA" in str(e).upper():
            print("\n  TIP: For Alpaca, set environment variables:")
            print("    set ALPACA_API_KEY=your_key")
            print("    set ALPACA_SECRET_KEY=your_secret")
        elif "IB" in str(e).upper() or "TWS" in str(e).upper() or "connect" in str(e).lower():
            print("\n  TIP: For IB TWS:")
            print("    1. Start IB TWS or IB Gateway")
            print("    2. Login with paper trading credentials")
            print("    3. Enable API: Configure -> API -> Settings")
            print("       - Check 'Enable ActiveX and Socket Clients'")
            print("       - Socket port: 7497 (paper)")
            print("    4. Retry this command")
        print("\n  Or use Yahoo Finance (no account needed):")
        print("    python scripts/live_paper_trade.py --data-source yahoo")
        sys.exit(1)

    # Run
    if args.mode == "once":
        summary = runner.run_once(date=args.backfill_date)
        if summary.get("status") in ("skipped", "no_data"):
            print(f"\n  {summary.get('reason', 'No action taken')}")
            print(f"  Status: {summary.get('status')}")
        else:
            runner._print_summary(summary)
    else:
        runner.run_continuous(interval_minutes=args.interval)

    print("\nDone.")


if __name__ == "__main__":
    main()
