#!/usr/bin/env python3
"""CSV -> NautilusTrader bars + the SIM crypto-perpetual instrument.

Defines ETHUSDT-PERP.SIM (mirrors the Binance ETHUSDT perp test instrument but
on the SIM venue, with taker fee 0.0005 to match the reference engine) and
loads data/sample/ETH_USDT_4h.csv into 4-HOUR-LAST bars via BarDataWrangler.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd

from nautilus_trader.model.currencies import ETH, USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments.crypto_perpetual import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = PROJECT_ROOT / "data" / "sample" / "ETH_USDT_4h.csv"

VENUE = Venue("SIM")
INSTRUMENT_ID = InstrumentId(Symbol("ETHUSDT-PERP"), VENUE)
BAR_TYPE_STR = f"{INSTRUMENT_ID}-4-HOUR-LAST-EXTERNAL"


def make_instrument() -> CryptoPerpetual:
    """ETHUSDT perpetual on the SIM venue, taker fee 0.0005 (reference parity)."""
    return CryptoPerpetual(
        instrument_id=INSTRUMENT_ID,
        raw_symbol=Symbol("ETHUSDT-PERP"),
        base_currency=ETH,
        quote_currency=USDT,
        settlement_currency=USDT,
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.001"),
        max_quantity=Quantity.from_str("1000000"),
        min_quantity=Quantity.from_str("0.001"),
        max_notional=None,
        min_notional=Money(1.00, USDT),
        max_price=Price.from_str("1000000.00"),
        min_price=Price.from_str("0.01"),
        margin_init=Decimal("1.00"),   # margin = notional / leverage (leverage set on venue)
        margin_maint=Decimal("0.35"),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),   # reference engine fee_rate per side
        ts_event=0,
        ts_init=0,
    )


def make_bar_type() -> BarType:
    return BarType.from_str(BAR_TYPE_STR)


def load_dataframe(csv_path: Path = CSV_PATH) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = [c.lower() for c in df.columns]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def load_bars(instrument: CryptoPerpetual, bar_type: BarType, csv_path: Path = CSV_PATH):
    df = load_dataframe(csv_path)
    wrangler = BarDataWrangler(bar_type, instrument)
    return wrangler.process(df)


if __name__ == "__main__":
    inst = make_instrument()
    bt = make_bar_type()
    bars = load_bars(inst, bt)
    print(f"Instrument: {inst.id} taker_fee={inst.taker_fee}")
    print(f"Bar type:   {bt}")
    print(f"Bars:       {len(bars)}")
    print(f"First:      {bars[0]}")
    print(f"Last:       {bars[-1]}")
