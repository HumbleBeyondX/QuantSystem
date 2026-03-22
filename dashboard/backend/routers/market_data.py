"""Real-time Market Data API endpoints.

Uses Alpaca API for real-time US equity data (quotes, bars, trades, snapshots).
Falls back to yfinance for historical data and non-Alpaca assets.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()

# ---------------------------------------------------------------------------
# Alpaca client singleton
# ---------------------------------------------------------------------------
_alpaca_data_client = None
_alpaca_trading_client = None


def _get_alpaca_clients():
    """Lazy-init Alpaca clients."""
    global _alpaca_data_client, _alpaca_trading_client
    if _alpaca_data_client is None:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        api_key = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")

        if not api_key or not secret_key:
            return None, None

        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient

            _alpaca_data_client = StockHistoricalDataClient(
                api_key=api_key, secret_key=secret_key
            )
            _alpaca_trading_client = TradingClient(
                api_key=api_key, secret_key=secret_key, paper=True
            )
        except Exception as e:
            print(f"[market_data] Alpaca init failed: {e}")
            return None, None

    return _alpaca_data_client, _alpaca_trading_client


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------
class QuoteResponse(BaseModel):
    symbol: str
    price: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    prev_close: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    timestamp: Optional[str] = None
    source: str = "alpaca"


class OHLCVBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None


class TradeRecord(BaseModel):
    timestamp: str
    price: float
    size: float
    conditions: Optional[List[str]] = None


class WatchlistItem(BaseModel):
    symbol: str
    price: float
    change: float
    change_pct: float
    volume: float
    source: str = "alpaca"


class MarketOverview(BaseModel):
    indices: List[WatchlistItem]
    watchlist: List[WatchlistItem]
    last_updated: str


# ---------------------------------------------------------------------------
# Helper: get snapshot from Alpaca (most efficient - single call per symbol)
# ---------------------------------------------------------------------------
def _get_snapshots(symbols: List[str]) -> Dict[str, Any]:
    """Get Alpaca snapshots for multiple symbols at once."""
    data_client, _ = _get_alpaca_clients()
    if not data_client:
        return {}

    try:
        from alpaca.data.requests import StockSnapshotRequest
        req = StockSnapshotRequest(symbol_or_symbols=symbols)
        return data_client.get_stock_snapshot(req)
    except Exception as e:
        print(f"[market_data] Snapshot error: {e}")
        return {}


def _snapshot_to_quote(symbol: str, snap) -> dict:
    """Convert Alpaca snapshot to quote dict."""
    daily = snap.daily_bar
    prev = snap.previous_daily_bar
    latest_trade = snap.latest_trade
    latest_quote = snap.latest_quote

    price = latest_trade.price if latest_trade else daily.close
    prev_close = prev.close if prev else daily.open
    change = price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0

    return {
        "symbol": symbol,
        "price": float(price),
        "change": round(float(change), 4),
        "change_pct": round(float(change_pct), 4),
        "open": float(daily.open),
        "high": float(daily.high),
        "low": float(daily.low),
        "close": float(daily.close),
        "volume": float(daily.volume),
        "prev_close": float(prev_close),
        "bid": float(latest_quote.bid_price) if latest_quote and latest_quote.bid_price and float(latest_quote.bid_price) > 0 else None,
        "ask": float(latest_quote.ask_price) if latest_quote and latest_quote.ask_price and float(latest_quote.ask_price) > 0 else None,
        "bid_size": float(latest_quote.bid_size) if latest_quote and latest_quote.bid_size and float(latest_quote.bid_size) > 0 else None,
        "ask_size": float(latest_quote.ask_size) if latest_quote and latest_quote.ask_size and float(latest_quote.ask_size) > 0 else None,
        "timestamp": str(latest_trade.timestamp) if latest_trade else str(daily.timestamp),
        "source": "alpaca",
    }


def _yfinance_quote(symbol: str) -> dict:
    """Fallback: get quote via yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        hist = ticker.history(period="2d")

        if hist.empty:
            raise ValueError(f"No data for {symbol}")

        last_row = hist.iloc[-1]
        prev_close = hist.iloc[-2]["Close"] if len(hist) > 1 else last_row["Open"]
        price = last_row["Close"]
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        return {
            "symbol": symbol,
            "price": round(float(price), 4),
            "change": round(float(change), 4),
            "change_pct": round(float(change_pct), 4),
            "open": round(float(last_row["Open"]), 4),
            "high": round(float(last_row["High"]), 4),
            "low": round(float(last_row["Low"]), 4),
            "close": round(float(last_row["Close"]), 4),
            "volume": float(last_row["Volume"]),
            "prev_close": round(float(prev_close), 4),
            "bid": None,
            "ask": None,
            "bid_size": None,
            "ask_size": None,
            "timestamp": str(last_row.name),
            "source": "yfinance",
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Symbol not found: {symbol} ({e})")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/quote/{symbol}", response_model=QuoteResponse)
async def get_quote(symbol: str):
    """Get real-time quote for a symbol.

    Tries Alpaca first, falls back to yfinance.
    """
    symbol = symbol.upper().strip()

    # Try Alpaca first
    snapshots = _get_snapshots([symbol])
    if symbol in snapshots:
        return _snapshot_to_quote(symbol, snapshots[symbol])

    # Fallback to yfinance
    return _yfinance_quote(symbol)


@router.get("/quotes/batch")
async def get_batch_quotes(
    symbols: str = Query(..., description="Comma-separated symbols, e.g. SPY,AAPL,MSFT")
):
    """Get quotes for multiple symbols at once (efficient batch call)."""
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        raise HTTPException(status_code=400, detail="No symbols provided")
    if len(sym_list) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 symbols per batch")

    results = []

    # Try Alpaca batch snapshot
    snapshots = _get_snapshots(sym_list)
    found_symbols = set()

    for sym in sym_list:
        if sym in snapshots:
            results.append(_snapshot_to_quote(sym, snapshots[sym]))
            found_symbols.add(sym)

    # Fallback to yfinance for missing symbols
    missing = [s for s in sym_list if s not in found_symbols]
    for sym in missing:
        try:
            results.append(_yfinance_quote(sym))
        except HTTPException:
            results.append({
                "symbol": sym,
                "price": 0,
                "change": 0,
                "change_pct": 0,
                "open": 0, "high": 0, "low": 0, "close": 0,
                "volume": 0,
                "prev_close": 0,
                "source": "error",
                "error": f"No data for {sym}",
            })

    return {"quotes": results, "count": len(results)}


@router.get("/ohlcv/{symbol}", response_model=List[OHLCVBar])
async def get_ohlcv(
    symbol: str,
    timeframe: str = Query("1Day", description="1Min, 5Min, 15Min, 1Hour, 1Day"),
    days: int = Query(30, description="Number of days of history"),
    limit: int = Query(500, description="Max number of bars"),
):
    """Get historical OHLCV bars for charting."""
    symbol = symbol.upper().strip()
    data_client, _ = _get_alpaca_clients()

    if data_client:
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            tf_map = {
                "1Min": TimeFrame.Minute,
                "5Min": TimeFrame(5, "Min"),
                "15Min": TimeFrame(15, "Min"),
                "1Hour": TimeFrame.Hour,
                "1Day": TimeFrame.Day,
            }
            tf = tf_map.get(timeframe, TimeFrame.Day)

            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=datetime.now() - timedelta(days=days),
                limit=limit,
            )
            bars = data_client.get_stock_bars(req)

            result = []
            if symbol in bars:
                for b in bars[symbol]:
                    result.append(OHLCVBar(
                        timestamp=str(b.timestamp),
                        open=float(b.open),
                        high=float(b.high),
                        low=float(b.low),
                        close=float(b.close),
                        volume=float(b.volume),
                        vwap=float(b.vwap) if b.vwap else None,
                    ))

            if result:
                return result
        except Exception as e:
            print(f"[market_data] Alpaca OHLCV error for {symbol}: {e}")

    # Fallback: yfinance
    try:
        import yfinance as yf
        period_map = {
            "1Min": "5d",
            "5Min": "60d",
            "15Min": "60d",
            "1Hour": "730d",
            "1Day": f"{days}d" if days <= 365 else "max",
        }
        interval_map = {
            "1Min": "1m",
            "5Min": "5m",
            "15Min": "15m",
            "1Hour": "1h",
            "1Day": "1d",
        }
        ticker = yf.Ticker(symbol)
        hist = ticker.history(
            period=period_map.get(timeframe, f"{days}d"),
            interval=interval_map.get(timeframe, "1d"),
        )
        result = []
        for idx, row in hist.iterrows():
            result.append(OHLCVBar(
                timestamp=str(idx),
                open=round(float(row["Open"]), 4),
                high=round(float(row["High"]), 4),
                low=round(float(row["Low"]), 4),
                close=round(float(row["Close"]), 4),
                volume=float(row["Volume"]),
            ))
        return result[-limit:]
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"OHLCV data error: {e}")


@router.get("/trades/{symbol}", response_model=List[TradeRecord])
async def get_recent_trades(
    symbol: str,
    limit: int = Query(50, description="Number of recent trades"),
):
    """Get recent trades for a symbol from Alpaca."""
    symbol = symbol.upper().strip()
    data_client, _ = _get_alpaca_clients()

    if not data_client:
        raise HTTPException(status_code=503, detail="Alpaca not configured")

    try:
        from alpaca.data.requests import StockTradesRequest
        from alpaca.data.enums import DataFeed

        # Go back enough days to find trading hours (weekends, holidays)
        now = datetime.utcnow()
        start = now - timedelta(days=5)

        req = StockTradesRequest(
            symbol_or_symbols=symbol,
            start=start,
            end=now,
            limit=limit,
            feed=DataFeed.IEX,  # Free plan uses IEX feed
        )
        trades_data = data_client.get_stock_trades(req)

        result = []
        # Access via .data dict to avoid KeyError on __getitem__
        trade_list = trades_data.data.get(symbol, []) if hasattr(trades_data, 'data') else []
        for t in trade_list[-limit:]:
            result.append(TradeRecord(
                timestamp=str(t.timestamp),
                price=float(t.price),
                size=float(t.size),
                conditions=list(t.conditions) if t.conditions else None,
            ))

        # Return most recent first
        result.reverse()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Trades error: {e}")


@router.get("/overview")
async def get_market_overview():
    """Get market overview: major indices + popular stocks."""
    indices = ["SPY", "QQQ", "DIA", "IWM", "VXX"]
    popular = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "JPM"]

    all_symbols = indices + popular
    snapshots = _get_snapshots(all_symbols)

    def make_item(sym: str) -> dict:
        if sym in snapshots:
            q = _snapshot_to_quote(sym, snapshots[sym])
            return {
                "symbol": sym,
                "price": q["price"],
                "change": q["change"],
                "change_pct": q["change_pct"],
                "volume": q["volume"],
                "source": "alpaca",
            }
        # yfinance fallback
        try:
            q = _yfinance_quote(sym)
            return {
                "symbol": sym,
                "price": q["price"],
                "change": q["change"],
                "change_pct": q["change_pct"],
                "volume": q["volume"],
                "source": "yfinance",
            }
        except Exception:
            return {
                "symbol": sym,
                "price": 0,
                "change": 0,
                "change_pct": 0,
                "volume": 0,
                "source": "error",
            }

    return {
        "indices": [make_item(s) for s in indices],
        "watchlist": [make_item(s) for s in popular],
        "last_updated": datetime.utcnow().isoformat(),
    }


@router.get("/search/{query}")
async def search_symbols(query: str, limit: int = Query(10)):
    """Search for symbols by name or ticker.

    Uses yfinance search since Alpaca doesn't have a search endpoint.
    Also includes a curated list of popular symbols for fast matching.
    """
    query = query.upper().strip()

    # Curated popular symbols for fast matching
    popular = {
        "SPY": "SPDR S&P 500 ETF",
        "QQQ": "Invesco QQQ Trust",
        "DIA": "SPDR Dow Jones ETF",
        "IWM": "iShares Russell 2000 ETF",
        "VXX": "iPath VIX Short-Term",
        "GLD": "SPDR Gold Shares",
        "SLV": "iShares Silver Trust",
        "TLT": "iShares 20+ Year Treasury",
        "XLF": "Financial Select Sector SPDR",
        "XLE": "Energy Select Sector SPDR",
        "XLK": "Technology Select Sector SPDR",
        "ARKK": "ARK Innovation ETF",
        "AAPL": "Apple Inc.",
        "MSFT": "Microsoft Corporation",
        "GOOGL": "Alphabet Inc.",
        "AMZN": "Amazon.com Inc.",
        "TSLA": "Tesla Inc.",
        "NVDA": "NVIDIA Corporation",
        "META": "Meta Platforms Inc.",
        "JPM": "JPMorgan Chase & Co.",
        "V": "Visa Inc.",
        "JNJ": "Johnson & Johnson",
        "WMT": "Walmart Inc.",
        "PG": "Procter & Gamble Co.",
        "UNH": "UnitedHealth Group",
        "HD": "Home Depot Inc.",
        "BAC": "Bank of America Corp.",
        "MA": "Mastercard Inc.",
        "DIS": "Walt Disney Co.",
        "NFLX": "Netflix Inc.",
        "AMD": "Advanced Micro Devices",
        "INTC": "Intel Corporation",
        "CRM": "Salesforce Inc.",
        "ADBE": "Adobe Inc.",
        "PYPL": "PayPal Holdings",
        "COIN": "Coinbase Global",
        "PLTR": "Palantir Technologies",
        "SOFI": "SoFi Technologies",
        "RIVN": "Rivian Automotive",
        "NIO": "NIO Inc.",
    }

    # Match against curated list
    matches = []
    for sym, name in popular.items():
        if query in sym or query in name.upper():
            matches.append({"symbol": sym, "name": name})
            if len(matches) >= limit:
                break

    # Also try yfinance search for additional results
    if len(matches) < limit:
        try:
            import yfinance as yf
            ticker = yf.Ticker(query)
            info = ticker.fast_info
            if info and hasattr(info, 'last_price') and info.last_price:
                if not any(m["symbol"] == query for m in matches):
                    matches.insert(0, {"symbol": query, "name": query})
        except Exception:
            pass

    return {"results": matches[:limit], "query": query}


@router.get("/prices/{symbol}")
async def get_price(symbol: str):
    """Legacy endpoint - redirects to quote."""
    symbol = symbol.upper().strip()

    snapshots = _get_snapshots([symbol])
    if symbol in snapshots:
        q = _snapshot_to_quote(symbol, snapshots[symbol])
        return {"symbol": symbol, "price": q["price"], "source": "alpaca"}

    try:
        q = _yfinance_quote(symbol)
        return {"symbol": symbol, "price": q["price"], "source": "yfinance"}
    except Exception:
        return {"symbol": symbol, "price": 0, "source": "not_connected"}
