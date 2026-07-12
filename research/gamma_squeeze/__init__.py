"""Gamma squeeze detection pipeline.

Tools for identifying short-dated gamma-squeeze setups in retail-favorite
single-name equities using only free data (yfinance + FINRA).

Public API:
    from research.gamma_squeeze import GammaSqueezeScanner, load_universe
    scanner = GammaSqueezeScanner()
    candidates = scanner.scan_universe(load_universe())
    print(candidates.head(10))

LIMITATIONS — read deploy/INSTALL.md or the user docs:
  * yfinance only returns the CURRENT snapshot of option chains, never history.
    This means: no proper backtest is possible. Validate forward only.
  * No intraday data — by the time the daily scan runs, an intraday squeeze
    may already be over.
  * Short-interest data lags real positioning by 2 weeks (FINRA bi-monthly).
  * Black-Scholes gamma assumes log-normal returns which is a poor fit for
    short-dated OTM options on tail names. The GEX proxy is INDICATIVE
    only, not a precise number.
"""

from research.gamma_squeeze.scanner import (  # noqa: F401
    GammaSqueezeScanner,
    SqueezeCandidate,
    load_universe,
)
