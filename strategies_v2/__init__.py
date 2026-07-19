"""Runtime-v2 strategies. Import registers into REGISTRY.

The three ORIGINAL paper strategies (scripts/) are not here — they stay
untouched until shadow-parity cutover.
"""
REGISTRY: dict = {}


def register(cls):
    REGISTRY[cls.id] = cls
    return cls


from strategies_v2 import scalp_trend  # noqa: E402,F401
from strategies_v2 import funding_carry  # noqa: E402,F401
from strategies_v2 import market_maker  # noqa: E402,F401
