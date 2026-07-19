"""OrderIntent — the only way a strategy expresses a desire to trade.

Strategies never touch venues. They return Intents from on_tick();
RiskRails filters/sizes them; a FillEngine turns survivors into fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Intent:
    action: str                    # "open" | "close" | "quote" | "cancel_quotes"
    symbol: str
    side: Optional[str] = None     # "long" | "short" (open/quote)
    qty: Optional[float] = None    # base units; None + stop_dist => rails size it
    qty_usd: Optional[float] = None
    price: Optional[float] = None  # None => taker at book; set + maker => post
    maker: bool = False
    stop_dist: Optional[float] = None    # $ distance; rails clamp vs liquidation
    trail_dist: Optional[float] = None   # $ trail distance (engine-managed)
    trail_activate: Optional[float] = None  # $ profit before trail arms
    max_hold_ticks: Optional[int] = None
    reduce_only: bool = False
    tag: str = ""
    meta: dict = field(default_factory=dict)
