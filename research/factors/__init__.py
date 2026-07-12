"""Factor library for single-asset time-series research.

Two coexisting abstractions:

* ``AlphaFactor`` (in ``factor_base.py``) — cross-sectional factors for
  equities/crypto baskets, returns Series indexed by symbol at a point in time.

* ``TimeSeriesFactor`` (in ``ts_factor_base.py``) — single-asset time-series
  factors for crypto 1H research. Returns Series indexed by timestamp.
  This is the abstraction used by the new ``ETHRegimeStrategy``.

Factor registration:

    from research.factors.ts_factor_base import register_factor, get_all_factors

    @register_factor
    class MyFactor(TimeSeriesFactor):
        ...

    factors = get_all_factors()  # all registered time-series factors
"""

from research.factors.ts_factor_base import (  # noqa: F401
    TimeSeriesFactor,
    FactorOutput,
    get_all_factors,
    get_factor,
    register_factor,
)
