"""Strategy base contract for the unified runtime.

A strategy is ONE class that declares what it needs and emits Intents.
The same instance logic runs in backtest, paper and (later) live —
parity is architectural, not disciplinary.

params_schema drives the dashboard's auto-generated parameter form
(sliders/inputs) — retuning never requires code edits.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional

from core.runtime.intents import Intent


@dataclass
class ParamSpec:
    name: str
    type: str                  # "float" | "int" | "bool" | "choice"
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    choices: Optional[list] = None
    help: str = ""


class Strategy(ABC):
    """Subclass contract:

    class MyStrat(Strategy):
        id = "my_strat"
        book_type = "linear"          # linear | options | mm | carry
        asset_domain = "tokenized"    # spy | btc | tokenized | ...
        cadence_sec = 300
        universe = ["NVDA", "TSLA"]
        primary_tf = "5m"
        params_schema = [ParamSpec("atr_stop", "float", 1.5, 0.5, 4.0, 0.25)]

        def on_tick(self, ctx) -> list[Intent]: ...
    """

    id: ClassVar[str]
    book_type: ClassVar[str]
    asset_domain: ClassVar[str]
    cadence_sec: ClassVar[int] = 300
    universe: ClassVar[List[str]] = []
    primary_tf: ClassVar[str] = "5m"
    params_schema: ClassVar[List[ParamSpec]] = []

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.p: Dict[str, Any] = {s.name: s.default for s in self.params_schema}
        for k, v in (params or {}).items():
            if k in self.p:
                self.p[k] = v

    @abstractmethod
    def on_tick(self, ctx) -> List[Intent]:
        """Called once per cadence with a MarketContext. Return Intents."""

    def warmup_bars(self) -> int:
        """Bars of history the strategy needs before first signal."""
        return 60
