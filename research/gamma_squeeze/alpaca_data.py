"""Alpaca OPRA options-chain provider for the gamma squeeze scanner.

Replaces yfinance chains with exchange-grade data (user directive 2026-07:
no synthetic greeks where real ones exist):

  * greeks + IV  → Data API option-chain snapshots (OPRA, computed upstream)
  * open interest → Trading API option-contracts endpoint (snapshots don't
    carry OI, so the two sources are joined on the OCC option symbol)

Free tier uses the indicative feed — fine for daily scanning. ~2-4 requests
per underlying, well inside the 200 req/min limit for a 50-name universe.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from core.logging import get_logger

logger = get_logger(__name__)


class AlpacaOptionsProvider:
    """Thin wrapper joining Alpaca contract specs (OI) with snapshots (greeks)."""

    def __init__(self,
                 api_key: Optional[str] = None,
                 secret_key: Optional[str] = None) -> None:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.trading.client import TradingClient

        try:  # CLI runs don't preload the backend's env
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._secret = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        if not self._api_key or not self._secret:
            raise ValueError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")

        self._data = OptionHistoricalDataClient(self._api_key, self._secret)
        self._trading = TradingClient(self._api_key, self._secret, paper=True)

    # ------------------------------------------------------------------
    def get_chain(self, underlying: str, max_dte: int = 60) -> pd.DataFrame:
        """Full option chain under `max_dte`, one row per contract.

        Columns: symbol, type ('call'/'put'), strike, expiry, dte,
                 openInterest, impliedVolatility, gamma, delta, bid, ask.
        Empty DataFrame when the underlying has no listed options.
        """
        exp_lte = date.today() + timedelta(days=max_dte)

        # ── 1. Contract specs + open interest (Trading API, paginated) ──
        from alpaca.trading.requests import GetOptionContractsRequest

        contracts: dict[str, dict] = {}
        page_token = None
        while True:
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date_lte=exp_lte,
                limit=10_000,
                page_token=page_token,
            )
            resp = self._trading.get_option_contracts(req)
            batch = resp.option_contracts or []
            for c in batch:
                contracts[c.symbol] = {
                    "type": str(c.type.value if hasattr(c.type, "value") else c.type),
                    "strike": float(c.strike_price),
                    "expiry": c.expiration_date,
                    "openInterest": float(c.open_interest or 0),
                }
            page_token = getattr(resp, "next_page_token", None)
            if not page_token or not batch:
                break

        if not contracts:
            return pd.DataFrame()

        # ── 2. Snapshots: greeks + IV + quotes (Data API) ──
        from alpaca.data.requests import OptionChainRequest

        snapshots = self._data.get_option_chain(OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_lte=exp_lte,
        ))

        rows = []
        today = date.today()
        for sym, spec in contracts.items():
            snap = snapshots.get(sym)
            iv = gamma = delta = 0.0
            bid = ask = 0.0
            if snap is not None:
                iv = float(snap.implied_volatility or 0)
                if snap.greeks is not None:
                    gamma = float(snap.greeks.gamma or 0)
                    delta = float(snap.greeks.delta or 0)
                if snap.latest_quote is not None:
                    bid = float(snap.latest_quote.bid_price or 0)
                    ask = float(snap.latest_quote.ask_price or 0)
            rows.append({
                "symbol": sym,
                "type": spec["type"],
                "strike": spec["strike"],
                "expiry": spec["expiry"],
                "dte": (spec["expiry"] - today).days,
                "openInterest": spec["openInterest"],
                "impliedVolatility": iv,
                "gamma": gamma,
                "delta": delta,
                "bid": bid,
                "ask": ask,
            })

        df = pd.DataFrame(rows)
        df = df[df["dte"] >= 1].reset_index(drop=True)
        logger.debug("alpaca_chain_fetched", underlying=underlying,
                     contracts=len(df),
                     with_greeks=int((df["gamma"] != 0).sum()))
        return df
