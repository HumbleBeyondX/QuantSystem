"""QuantSystem Strategy Runtime — unified paper/live/backtest host.

One Strategy class runs in three hosts with identical mechanics:
  * PaperRunner   (core/runtime/runner.py)  — live shadow fills
  * BacktestHost  (core/runtime/backtest_host.py) — catalog replay
  * (future) live execution — FillEngine swapped for a real executor

The three ORIGINAL paper strategies (spy_gamma_scalp, btc_wheel,
btc_wheel_delta in scripts/) are NOT hosted here — they keep running
untouched until shadow-parity cutover (see
docs/architecture/unified_execution_refactor.md).
"""
from core.runtime.intents import Intent  # noqa: F401
from core.runtime.strategy import ParamSpec, Strategy  # noqa: F401
