"""Type aliases and custom types used across the QuantSystem."""

from typing import Any, Dict, List, NewType

# Symbol string (e.g., "AAPL", "BTC/USDT")
Symbol = NewType("Symbol", str)

# Strategy identifier
StrategyId = NewType("StrategyId", str)

# JSON-serializable dict
JsonDict = Dict[str, Any]

# Price/quantity as float (could be Decimal in ultra-precise contexts)
Price = NewType("Price", float)
Quantity = NewType("Quantity", float)

# Feature vector: feature_name -> value
FeatureVector = Dict[str, float]

# Portfolio weights: symbol -> weight [0, 1]
PortfolioWeights = Dict[str, float]

# Target positions: symbol -> target_quantity
TargetPositions = Dict[str, float]
