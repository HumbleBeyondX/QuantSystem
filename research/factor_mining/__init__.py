"""Enterprise factor mining pipeline.

Components:
  data_panel      — DataPanel: multi-asset OHLCV wrapper
  operators       — primitive operators (rank, ts_rank, delta, ...)
  base            — CrossSectionalFactor ABC + decorator registry
  factors/        — hand-curated factor library by category
  evaluator       — full industry-standard factor evaluation
  selector        — decorrelation + clustering for independent factors
  miner           — formula-based discovery (WorldQuant-style)
  persistence     — DB + Parquet caching
  reports         — markdown catalog + correlation heatmap

Public API:
"""
from research.factor_mining.data_panel import DataPanel  # noqa: F401
from research.factor_mining.base import (  # noqa: F401
    CrossSectionalFactor,
    register_factor,
    get_all_factors,
    get_factor,
    list_factor_names,
)
from research.factor_mining.evaluator import (  # noqa: F401
    FactorMetrics,
    evaluate_factor,
)
