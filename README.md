# QuantSystem

Production-grade quantitative trading system for **US Equities**, **Options**, and **Cryptocurrency** markets. Features a 7-layer modular architecture covering data ingestion, factor research, strategy engine, portfolio optimization, risk management, execution, and real-time monitoring.

```
data/ ─── L0-L1  Data connectors, feature store, streaming pipeline
research/ ── L2   Factor research, ML models, RL agents
strategies/ ─ L3  14 trading strategies (crypto, equity, options)
portfolio/ ── L4  Optimization (MVO, BL, HRP, RL) + 3-layer risk engine
execution/ ── L5  Paper & live execution (IB, CCXT, Alpaca)
backtesting/ ─    Event-driven + vectorized backtesting
dashboard/ ── L6  FastAPI backend + Next.js frontend
```

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Developing a New Strategy](#developing-a-new-strategy)
3. [Adding a New Data Source](#adding-a-new-data-source)
4. [Running Research & Factor Analysis](#running-research--factor-analysis)
5. [Running Backtests](#running-backtests)
6. [Paper Trading (Live Simulation)](#paper-trading-live-simulation)
7. [Dashboard: Backend & Frontend](#dashboard-backend--frontend)
8. [Debugging Guide](#debugging-guide)
9. [Project Structure](#project-structure)
10. [Configuration Reference](#configuration-reference)

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard frontend)
- Docker & Docker Compose (for infrastructure services)
- Conda (recommended): `conda create -n quantsystem python=3.11`

### Installation

```bash
# Clone the repo
git clone https://github.com/HumbleBeyondX/QuantSystem.git
cd QuantSystem

# Install Python dependencies
pip install -e ".[dev]"

# Set up environment variables
cp .env.example .env
# Edit .env with your API keys (Alpaca, IB, Polygon, Binance, etc.)

# Install frontend dependencies
cd dashboard/frontend && npm install && cd ../..

# Start infrastructure (Kafka, TimescaleDB, Redis, Grafana)
make up

# Initialize database
make init-db
```

### Run Everything

```bash
# Terminal 1: Backend API (port 8000)
make backend

# Terminal 2: Frontend UI (port 3000)
make frontend

# Terminal 3: Paper trading
python scripts/live_paper_trade.py --data-source alpaca --mode continuous --interval 60
```

Open http://localhost:3000 to view the dashboard.

---

## Developing a New Strategy

Every strategy inherits from `StrategyBase` and follows a consistent lifecycle. Here is a complete walkthrough.

### Step 1: Understand the Strategy Interface

All strategies implement this contract defined in `strategies/base.py`:

```python
class StrategyBase(ABC):
    """
    Lifecycle:
        1. initialize()                - Load models, warm up indicators
        2. on_bar() / on_tick()        - Receive market data
        3. generate_signals()          - Produce trading signals
        4. calculate_target_positions() - Convert signals to positions
        5. on_fill()                   - Handle order fills
        6. shutdown()                  - Save state, cleanup
    """

    def on_bar(self, symbol: str, bar: Bar) -> None: ...
    def on_tick(self, symbol: str, tick: Tick) -> None: ...
    def on_orderbook(self, symbol: str, orderbook: OrderBookSnapshot) -> None: ...

    @abstractmethod
    def generate_signals(self) -> List[Signal]: ...

    def calculate_target_positions(self) -> List[TargetPosition]: ...
```

### Step 2: Create the Strategy File

Choose the right directory based on asset class:

```
strategies/
  crypto/       # Crypto strategies
  equity/       # US equity strategies
  options/      # Options strategies
  ensemble/     # Multi-strategy combinations
```

Create your file, e.g., `strategies/crypto/my_mean_reversion.py`:

```python
"""Mean reversion strategy for crypto markets."""

from __future__ import annotations
from typing import List
import numpy as np

from core.enums import AssetClass, SignalDirection
from core.models import Bar, Signal
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_mean_reversion")
class MeanReversionStrategy(StrategyBase):
    """Buy when price drops below lower Bollinger Band, sell above upper."""

    def initialize(self) -> None:
        super().initialize()
        self._lookback = self.params.get("lookback", 20)
        self._num_std = self.params.get("num_std", 2.0)
        self._price_history: dict[str, list[float]] = {}

    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        hist = self._price_history.setdefault(symbol, [])
        hist.append(bar.close)
        # Keep only what we need
        if len(hist) > self._lookback * 2:
            self._price_history[symbol] = hist[-self._lookback * 2:]

    def generate_signals(self) -> List[Signal]:
        signals = []
        for symbol, prices in self._price_history.items():
            if len(prices) < self._lookback:
                continue

            arr = np.array(prices[-self._lookback:])
            mean = arr.mean()
            std = arr.std()
            current = prices[-1]

            if current < mean - self._num_std * std:
                signals.append(Signal(
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    strength=(mean - current) / (self._num_std * std),
                    metadata={"reason": "below_lower_band"},
                ))
            elif current > mean + self._num_std * std:
                signals.append(Signal(
                    symbol=symbol,
                    direction=SignalDirection.SHORT,
                    strength=(current - mean) / (self._num_std * std),
                    metadata={"reason": "above_upper_band"},
                ))
        return signals
```

### Step 3: Register the Strategy

Add your module to the discovery list in `strategies/registry.py`:

```python
def discover_strategies() -> None:
    modules = [
        # ... existing strategies ...
        "strategies.crypto.my_mean_reversion",  # <-- add this line
    ]
```

### Step 4: Create a YAML Config

Create `config/strategies/crypto_mean_reversion.yaml`:

```yaml
strategy_id: crypto_mean_reversion
name: Crypto Mean Reversion
asset_class: CRYPTO
symbols:
  - BTC/USDT
  - ETH/USDT
enabled: true
mode: paper

parameters:
  lookback: 20
  num_std: 2.0
  position_size_pct: 0.10    # 10% of equity per trade
  max_positions: 3
  stop_loss_pct: 0.03        # 3% stop loss
  take_profit_pct: 0.05      # 5% take profit
```

### Step 5: Add to Dashboard Strategy Catalog

To make it appear in the dashboard, add an entry in `dashboard/backend/routers/strategies.py` in the `STRATEGY_CATALOG` list:

```python
{
    "id": "crypto_mean_reversion",
    "name": "Crypto Mean Reversion",
    "asset_class": "crypto",
    "description": "Bollinger Band mean reversion on crypto pairs",
    "default_config": {"lookback": 20, "num_std": 2.0},
}
```

### Step 6: Backtest, Then Paper Trade, Then Go Live

```bash
# 1. Backtest first
python scripts/run_backtest.py \
    --strategy crypto_mean_reversion \
    --data-path data/sample/ \
    --start-date 2024-01-01 \
    --end-date 2024-12-31 \
    --initial-capital 100000

# 2. Paper trade to validate in real market conditions
python scripts/live_paper_trade.py --data-source alpaca --mode continuous

# 3. Only after validation, switch mode to "live" in config
```

---

## Adding a New Data Source

### Step 1: Understand the Connector Interface

All connectors implement `DataConnector` from `data/connectors/base.py`:

```python
class DataConnector(ABC):
    """
    Lifecycle: connect() -> subscribe() -> stream() -> unsubscribe() -> disconnect()
    """

    @property
    def name(self) -> str: ...           # Unique ID: e.g., "my_source"
    @property
    def asset_class(self) -> AssetClass: ...

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def subscribe(self, symbols, data_types) -> None: ...
    async def fetch_historical(self, symbol, timeframe, start, end) -> List[Bar]: ...
    async def stream(self) -> AsyncIterator[Bar]: ...
```

### Step 2: Create a New Connector

Create `data/connectors/my_source_connector.py`:

```python
"""Connector for MyDataSource API."""

from __future__ import annotations
from typing import AsyncIterator, List

from core.enums import AssetClass, TimeFrame
from core.models import Bar
from data.connectors.base import DataConnector


class MySourceConnector(DataConnector):

    @property
    def name(self) -> str:
        return "my_source"

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CRYPTO

    async def connect(self) -> None:
        # Initialize API client, authenticate
        self._client = MySourceAPI(api_key=os.getenv("MY_SOURCE_KEY"))
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.close()

    async def subscribe(self, symbols, data_types) -> None:
        for symbol in symbols:
            await self._client.subscribe(symbol)

    async def fetch_historical(self, symbol, timeframe, start, end) -> List[Bar]:
        raw = await self._client.get_candles(symbol, start, end)
        return [Bar(timestamp=r["t"], open=r["o"], high=r["h"],
                     low=r["l"], close=r["c"], volume=r["v"]) for r in raw]

    async def stream(self) -> AsyncIterator[Bar]:
        async for msg in self._client.ws_stream():
            yield Bar(...)
```

### Step 3: Wire It Into the Pipeline

Register your connector in `data/pipeline/ingest_manager.py` so the system can use it alongside existing connectors (IB, Polygon, CCXT, Alpaca, FRED).

### Existing Connectors Reference

| Connector | File | Asset Class | API Type |
|-----------|------|-------------|----------|
| Alpaca | `alpaca_connector.py` | US Equity | REST + Paper Trading |
| CCXT Pro | `ccxt_connector.py` | Crypto (130+ exchanges) | WebSocket |
| Interactive Brokers | `ib_connector.py` | Equity + Options | TWS API |
| Polygon.io | `polygon_connector.py` | US Equity | REST + WebSocket |
| FRED | `fred_connector.py` | Macro Data | REST |
| DEX | `dex_connector.py` | Crypto (on-chain) | RPC |
| News | `news_connector.py` | Sentiment | REST |

---

## Running Research & Factor Analysis

### Factor Research Pipeline

The research module (`research/`) follows this flow:

```
Data → Single Factor Test → Backtest Results → Factor Library
         ↑                                        ↓
      Refine & re-test                   Factor Combination (IC/IR)
                                                  ↓
                                          ML Model → Portfolio Optimization
```

### Step 1: Define a New Factor

Create a factor class in `research/factors/`:

```python
from research.factors.factor_base import FactorBase

class MyMomentumFactor(FactorBase):
    """12-1 month momentum factor."""

    def compute(self, prices: pd.DataFrame) -> pd.Series:
        return prices.pct_change(252).shift(21)  # Skip most recent month
```

### Step 2: Run Single-Factor Test

Use `research/factors/single_factor_test.py` to compute:
- **IC** (Information Coefficient) — correlation between factor and future returns
- **IR** (Information Ratio) — IC / std(IC), measures consistency
- **Quantile returns** — performance spread across factor quintiles
- **Factor decay** — how quickly the signal loses predictive power

### Step 3: Combine Factors

Use `research/factors/factor_combiner.py`:
- Equal-weight combination
- IC-weighted combination
- ML-based non-linear combination (LightGBM/XGBoost)

### Step 4: Train ML Models

Available models in `research/ml/`:

| Model | File | Use Case |
|-------|------|----------|
| LightGBM | `lightgbm_model.py` | Factor return prediction, feature importance |
| LSTM | `lstm_model.py` | Time series forecasting, volatility modeling |
| Transformer | `transformer_model.py` | Multi-asset cross-correlation modeling |

### Step 5: RL-Based Portfolio Optimization

Three custom Gym environments in `research/rl/envs/`:

```python
# Trading decisions for a single asset
from research.rl.envs.trading_env import TradingEnv

# Multi-asset portfolio allocation
from research.rl.envs.portfolio_env import PortfolioEnv

# Execution optimization (minimize market impact)
from research.rl.envs.execution_env import ExecutionEnv
```

Supported RL algorithms (via Stable-Baselines3): PPO, SAC, DDPG.

---

## Running Backtests

### CLI Backtest

```bash
python scripts/run_backtest.py \
    --strategy crypto_swing \
    --data-path data/sample/ \
    --start-date 2024-01-01 \
    --end-date 2024-12-31 \
    --initial-capital 100000
```

### Strategy-Specific Backtests

```bash
# Gamma Scalp strategy (options)
python scripts/backtest_gamma_scalp.py

# Optimize strategy parameters (random search)
python scripts/optimize_gamma_scalp.py
```

### Programmatic Usage

```python
from backtesting.engine.event_driven import EventDrivenBacktest
from backtesting.metrics.report import generate_text_report
from strategies.base import StrategyConfig
from strategies.registry import create_strategy, discover_strategies

# Discover all registered strategies
discover_strategies()

# Configure
config = StrategyConfig(
    strategy_id="crypto_mean_reversion",
    symbols=["BTC/USDT"],
    parameters={"lookback": 20, "num_std": 2.0},
)

# Create strategy instance
strategy = create_strategy("crypto_mean_reversion", config)

# Run backtest
bt = EventDrivenBacktest(strategy=strategy, data=historical_bars, initial_capital=100000)
results = bt.run()

# Print report
print(generate_text_report(results))
```

### Backtest Engines

| Engine | File | Speed | Fidelity |
|--------|------|-------|----------|
| Event-Driven | `backtesting/engine/event_driven.py` | Medium | High (realistic fills, slippage) |
| Vectorized | `backtesting/engine/vectorized.py` | Fast | Medium (bulk computation) |
| Tick Replay | `backtesting/engine/tick_replay.py` | Slow | Highest (tick-by-tick) |

### Metrics Computed

- **Returns**: Total, annualized, monthly breakdown
- **Risk-Adjusted**: Sharpe, Sortino, Calmar ratios
- **Drawdown**: Max drawdown, drawdown duration, recovery time
- **Trade Stats**: Win rate, profit factor, avg win/loss, max consecutive losses
- **Options**: Greeks P&L attribution (Delta, Gamma, Theta, Vega)
- **Risk**: VaR, CVaR at 95%/99% confidence

Results are saved to `results/` and viewable through the dashboard at `/backtest`.

---

## Paper Trading (Live Simulation)

Paper trading runs strategies against **real market data** with simulated execution. Always paper-trade before going live.

### Data Source Options

```bash
# Yahoo Finance - No account needed, simplest setup
python scripts/live_paper_trade.py --data-source yahoo --mode once

# Alpaca - Free paper trading API (recommended)
# Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env first
python scripts/live_paper_trade.py --data-source alpaca --mode once
python scripts/live_paper_trade.py --data-source alpaca --mode continuous --interval 60

# Interactive Brokers - Most realistic, requires TWS running
python scripts/live_paper_trade.py --data-source ib --mode once --ib-execution
```

### What Happens During Paper Trading

1. **State persistence**: Strategy state is saved to `data/live_state/*.pkl` (pickle files)
2. **Daily logs**: Market data + actions logged to `logs/paper_trading/*_daily.jsonl`
3. **Trade logs**: Individual trades logged to `logs/paper_trading/*_trades.jsonl`
4. **Dashboard sync**: The backend API reads these files to display real-time status

### Deploying Multiple Strategies

The system supports running multiple strategies simultaneously. Deploy via the dashboard Overview page, or manually edit `data/strategy_deployments.json`:

```json
[
  {
    "deploy_id": "spy_gamma_scalp_1",
    "strategy_id": "spy_gamma_scalp",
    "name": "SPY Gamma Scalp",
    "mode": "paper",
    "status": "running",
    "data_source": "alpaca",
    "initial_capital": 100000
  }
]
```

---

## Dashboard: Backend & Frontend

### Starting the Dashboard

```bash
# Backend (FastAPI, port 8000)
make backend
# or: uvicorn dashboard.backend.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend (Next.js, port 3000)
make frontend
# or: cd dashboard/frontend && npm run dev
```

### Dashboard Pages

| Page | URL | What It Shows |
|------|-----|---------------|
| **Overview** | `/` | Portfolio KPIs, deployed strategies list, deploy/stop/adjust controls, strategy catalog |
| **Portfolio** | `/portfolio` | Holdings, allocation breakdown, P&L attribution |
| **Strategies** | `/strategies` | Strategy browser, parameter configuration |
| **Trading** | `/trading` | Live paper trading monitor — equity curve, Greeks, positions, trade log. Strategy selector to switch between deployed strategies |
| **Backtest** | `/backtest` | Run backtests, view results, compare strategies |
| **Risk** | `/risk` | VaR/CVaR, Greeks exposure, drawdown, correlation matrix |
| **Market Data** | `/market` | Real-time quotes, charts, options chains |

### Backend API Endpoints

```
GET  /api/system/overview              # Aggregated portfolio metrics
GET  /api/system/health                # System health check

GET  /api/paper-trading/status         # Strategy status (accepts ?strategy_id=)
GET  /api/paper-trading/equity-curve   # Equity curve data
GET  /api/paper-trading/daily-log      # Daily activity log
GET  /api/paper-trading/trades         # Trade history
GET  /api/paper-trading/signal-history # Signal history

GET  /api/strategies/catalog           # Available strategies
GET  /api/strategies/                  # Deployed strategies
POST /api/strategies/deploy            # Deploy a new strategy
POST /api/strategies/{id}/start        # Start a strategy
POST /api/strategies/{id}/stop         # Stop a strategy
POST /api/strategies/{id}/adjust       # Adjust parameters
DELETE /api/strategies/{id}            # Remove deployment

GET  /api/portfolio/                   # Portfolio holdings
GET  /api/backtest/results             # Backtest results
GET  /api/market/quote                 # Market data quotes
GET  /api/risk/metrics                 # Risk metrics
```

### Multi-Strategy Routing

All `/api/paper-trading/*` endpoints accept a `strategy_id` query parameter:

```
GET /api/paper-trading/status?strategy_id=spy_gamma_scalp
GET /api/paper-trading/status?strategy_id=equity_factor
```

Each strategy reads from its own state/log files, so data is fully isolated.

### Infrastructure Monitoring

```
Prometheus:        http://localhost:9090
Grafana:           http://localhost:3001  (admin/admin)
Redpanda Console:  http://localhost:8180
MinIO Console:     http://localhost:9001  (minioadmin/minioadmin)
```

---

## Debugging Guide

### 1. Strategy Not Generating Signals

**Symptoms**: Strategy is "running" but no trades appear.

**Debug steps**:

```bash
# Check the daily log for signal values
cat logs/paper_trading/gamma_scalp_daily.jsonl | python -m json.tool | tail -20

# Look for the signal composite vs threshold
# Example: composite=0.18 < threshold=0.25 means signal isn't strong enough
```

**Common causes**:
- Signal threshold too high — lower `sell_signal_threshold` in config YAML
- VIX regime filter blocking — check if VIX is outside `vix_floor`/`vix_ceiling` range
- Dampener crushing signal — check VoV (vol-of-vol) dampener value in logs
- Insufficient data history — strategy needs N bars of warmup before generating signals

### 2. Backend API Returning Errors

**Symptoms**: Dashboard shows "Error fetching data" or blank panels.

**Debug steps**:

```bash
# Check if backend is running
curl http://localhost:8000/api/system/health

# Check backend logs
# Look for Python tracebacks in the terminal running uvicorn

# Test specific endpoint
curl http://localhost:8000/api/paper-trading/status?strategy_id=spy_gamma_scalp | python -m json.tool

# Common: state file doesn't exist yet
ls data/live_state/
# If empty, run paper trading once to generate state
```

**Common causes**:
- Backend not started — run `make backend`
- Missing state file — strategy hasn't run yet, endpoints return defaults
- Import error — check Python path and missing dependencies
- Port conflict — another process using port 8000/8001

### 3. Frontend Not Loading / Showing Stale Data

**Debug steps**:

```bash
# Check frontend is running
curl http://localhost:3000

# Check browser console (F12) for errors
# Common: CORS error = backend not running or wrong port

# Verify API connectivity from frontend
curl http://localhost:8000/api/system/overview
```

**Common causes**:
- Backend CORS not allowing frontend origin — check `main.py` CORS config
- API base URL mismatch — check `API` constant in page.tsx files
- Auto-poll interval too long — data refreshes every 15 seconds by default

### 4. Greeks Showing Zero

**Symptoms**: Portfolio Greeks (Delta, Gamma, Theta, Vega) display as 0 on Trading page.

**Debug steps**:

```python
# Load the state file directly
import pickle
with open("data/live_state/gamma_scalp_state.pkl", "rb") as f:
    state = pickle.load(f)

print(f"Delta: {state.get('portfolio_delta')}")
print(f"Gamma: {state.get('portfolio_gamma')}")
print(f"Theta: {state.get('portfolio_theta')}")
print(f"Vega:  {state.get('portfolio_vega')}")
print(f"Position open: {state.get('position_open')}")
```

**Common causes**:
- No position open — Greeks are 0 when flat (correct behavior)
- Greeks only computed on next bar — if position just opened, wait for next data event
- State file stale — check `last_bar_date` in state

### 5. Paper Trading Script Crashes

**Debug steps**:

```bash
# Run with verbose logging
python scripts/live_paper_trade.py --data-source alpaca --mode once 2>&1 | tee debug.log

# Check for API key issues
python -c "import os; print(os.getenv('ALPACA_API_KEY', 'NOT SET'))"

# Check network connectivity to broker
curl -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
     https://paper-api.alpaca.markets/v2/account
```

**Common causes**:
- Missing API keys — check `.env` file
- Market closed — some data sources return empty outside trading hours
- Rate limiting — add delays between API calls
- Pickle corruption — delete `data/live_state/*.pkl` and restart

### 6. Backtest Results Don't Match Live

**Debug steps**:
- **Look-ahead bias**: Ensure features use only past data. Check `available_date` vs `observation_date`
- **Survivorship bias**: Historical data should include delisted stocks
- **Slippage model**: Compare backtest slippage settings with actual fill reports
- **Fee model**: Verify commission rates match your broker's actual rates

```bash
# Compare backtest config vs live config
diff config/strategies/spy_gamma_scalp_optimized.yaml \
     data/live_state/gamma_scalp_config.json
```

### 7. Docker Infrastructure Issues

```bash
# Check service status
docker compose ps

# View specific service logs
make logs-svc SVC=timescaledb
make logs-svc SVC=redpanda

# Restart everything
make down && make up

# Check resource usage
docker stats
```

### General Debugging Tips

1. **Always check logs first**: `logs/paper_trading/` has JSONL files with every decision
2. **Use the API directly**: `curl` endpoints before suspecting frontend issues
3. **Inspect state files**: Pickle files in `data/live_state/` contain full strategy state
4. **Compare with backtest**: Run the same date range in backtest mode to isolate live-specific issues
5. **One change at a time**: When tuning parameters, change one thing and observe

---

## Project Structure

```
QuantSystem/
├── core/                       # Domain models, enums, config, utilities
├── data/
│   ├── connectors/             # 7 data source connectors (IB, Alpaca, CCXT, etc.)
│   ├── features/               # Feature store (technical, microstructure, fundamental)
│   ├── pipeline/               # Ingestion, validation, normalization, replay
│   ├── streaming/              # Kafka producer/consumer
│   └── storage/                # Redis (hot), TimescaleDB (warm), Parquet/S3 (cold)
├── strategies/
│   ├── base.py                 # StrategyBase ABC
│   ├── registry.py             # Strategy discovery & registration
│   ├── crypto/                 # 6 crypto strategies
│   ├── equity/                 # 3 equity strategies
│   ├── options/                # 5 options strategies (gamma scalp, delta neutral, etc.)
│   └── ensemble/               # Multi-strategy coordination
├── portfolio/
│   ├── optimization/           # MVO, Black-Litterman, Risk Parity, HRP, RL
│   ├── risk/                   # Pre-trade checks, real-time monitor, circuit breaker
│   └── position/               # Position tracking & reconciliation
├── execution/
│   ├── engine/                 # Paper, IB, CCXT execution engines
│   ├── oms/                    # Order management system
│   └── models/                 # Slippage & fee models
├── backtesting/
│   ├── engine/                 # Event-driven, vectorized, tick replay
│   └── metrics/                # Performance, risk metrics, report generation
├── research/
│   ├── factors/                # Factor library, single-factor testing, combination
│   ├── ml/                     # LightGBM, LSTM, Transformer models
│   ├── rl/                     # PPO/SAC/DDPG agents + Gym environments
│   └── notebooks/              # Jupyter notebooks for exploration
├── dashboard/
│   ├── backend/                # FastAPI (routers, schemas, WebSocket)
│   ├── frontend/               # Next.js 14 + Tailwind CSS (dark theme)
│   └── alerts/                 # Telegram & Discord notification bots
├── monitoring/                 # Prometheus + Grafana configs
├── scripts/                    # CLI runners (backtest, paper trade, data fetch)
├── config/                     # YAML configs (strategies, brokers, risk limits)
├── tests/                      # Unit + integration tests
├── Makefile                    # Build automation shortcuts
├── docker-compose.yml          # Infrastructure services
├── pyproject.toml              # Python dependencies & tool configs
└── .env.example                # Environment variable template
```

---

## Configuration Reference

### Environment Variables (`.env`)

```bash
# Alpaca (paper trading - free)
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Interactive Brokers
IB_HOST=127.0.0.1
IB_PORT=7497              # 7497=Paper, 7496=Live

# Polygon.io
POLYGON_API_KEY=your_key

# Crypto exchanges
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret

# Infrastructure
TSDB_HOST=localhost        # TimescaleDB
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:19092
```

### Strategy Config (YAML)

Each strategy has a YAML file in `config/strategies/`. Example:

```yaml
strategy_id: spy_gamma_scalp
name: SPY Gamma Scalp v2
asset_class: EQUITY
symbols: [SPY]
enabled: true
mode: paper               # paper | live | backtest

parameters:
  dte: 5                  # Days to expiration
  base_contracts: 30      # Contracts per trade
  sell_signal_threshold: 0.25
  max_loss_pct: 0.20      # 20% hard stop
  max_drawdown_pct: 0.08  # 8% circuit breaker
  vix_floor: 12.0
  vix_ceiling: 40.0
```

### Makefile Commands

```
make up              # Start Docker infrastructure
make down            # Stop all services
make init-db         # Initialize database schema
make seed            # Seed historical data
make backend         # Start FastAPI (port 8000)
make frontend        # Start Next.js (port 3000)
make test            # Run all tests
make test-unit       # Unit tests only
make test-cov        # Tests with coverage report
make lint            # Ruff + MyPy checks
make fmt             # Auto-format code
make clean           # Remove cache files
```

---

## License

MIT
