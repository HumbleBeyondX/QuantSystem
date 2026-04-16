"""Data Layer API — Enterprise data management for quantitative strategies.

Provides:
  - Data source/connector discovery and status
  - Data inventory: what symbols, frequencies, time ranges are available
  - Collection jobs: trigger historical data fetches
  - Feature store: available computed features
  - Pipeline overview: batch vs streaming architecture
  - Real-time quote passthrough for market monitoring
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.paths import get_data_dir, get_resource_root  # noqa: E402

router = APIRouter()

# Settings file is user-editable (user dir), connectors/samples are bundled.
PROJECT_ROOT = get_resource_root()
CONFIG_FILE = get_data_dir().parent / "config" / "settings.yaml"
DATA_DIR = PROJECT_ROOT / "data"
CONNECTORS_DIR = DATA_DIR / "connectors"
SAMPLE_DIR = DATA_DIR / "sample"


# ── Schemas ────────────────────────────────────────────────────


class ConnectorStatus(str, Enum):
    online = "online"
    offline = "offline"
    configured = "configured"
    missing_keys = "missing_keys"


class DataSource(BaseModel):
    id: str
    name: str
    type: str  # "batch" | "streaming" | "hybrid"
    connector_class: str
    status: ConnectorStatus
    data_types: List[str]
    supported_assets: List[str]
    description: str
    config: Dict[str, Any] = {}
    last_checked: Optional[str] = None


class DatasetInfo(BaseModel):
    symbol: str
    asset_class: str
    source: str
    data_type: str  # ohlcv, tick, orderbook, feature
    frequency: str  # 1m, 5m, 1h, 1d, tick, realtime
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    records: Optional[int] = None
    storage_tier: str  # hot (Redis), warm (TimescaleDB), cold (Parquet)
    size_mb: Optional[float] = None
    freshness: str  # "live", "< 1h", "< 1d", "stale", "unknown"


class CollectionJob(BaseModel):
    id: str
    symbol: str
    source: str
    data_type: str
    frequency: str
    start_date: str
    end_date: str
    status: str  # "pending", "running", "completed", "failed"
    progress: float = 0.0
    records_fetched: int = 0
    created_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None


class CollectionRequest(BaseModel):
    symbol: str
    source: str = "ccxt"
    data_type: str = "ohlcv"
    frequency: str = "1h"
    start_date: str = ""
    end_date: str = ""
    exchange: str = "binance"


class FeatureInfo(BaseModel):
    name: str
    category: str  # technical, microstructure, fundamental, sentiment, macro
    description: str
    parameters: Dict[str, Any]
    frequency: str
    dependencies: List[str]


class PipelineInfo(BaseModel):
    name: str
    type: str  # "batch" | "streaming"
    source: str
    sink: str
    status: str
    throughput: Optional[str] = None
    description: str


# ── In-memory job store ──────────────────────────────────────

_collection_jobs: Dict[str, Dict[str, Any]] = {}


# ── Config loader ──────────────────────────────────────────────

def _load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


# ── Data source discovery ──────────────────────────────────────

def _check_env_key(key: str) -> bool:
    return bool(os.environ.get(key))


def _discover_sources() -> List[DataSource]:
    """Discover all configured data connectors and their status."""
    config = _load_config()
    connector_config = config.get("connectors", {})
    sources: List[DataSource] = []

    # 1. CCXT — Crypto exchanges
    ccxt_cfg = connector_config.get("ccxt", {})
    exchanges = ccxt_cfg.get("exchanges", ["binance", "okx", "bybit"])
    sources.append(DataSource(
        id="ccxt",
        name="CCXT Crypto Exchanges",
        type="hybrid",
        connector_class="CCXTConnector",
        status=ConnectorStatus.configured,
        data_types=ccxt_cfg.get("data_types", ["trade", "orderbook", "ohlcv"]),
        supported_assets=["crypto"],
        description=f"Unified access to {len(exchanges)} crypto exchanges ({', '.join(exchanges)}). "
                    "Supports real-time WebSocket streaming and REST historical data.",
        config={"exchanges": exchanges},
    ))

    # 2. Alpaca — US Equities
    has_alpaca = _check_env_key("ALPACA_API_KEY") or _check_env_key("APCA_API_KEY_ID")
    sources.append(DataSource(
        id="alpaca",
        name="Alpaca Markets",
        type="hybrid",
        connector_class="AlpacaConnector",
        status=ConnectorStatus.online if has_alpaca else ConnectorStatus.missing_keys,
        data_types=["quote", "bar", "trade", "snapshot"],
        supported_assets=["equity", "options"],
        description="US equities real-time data + paper/live trading. "
                    "IEX feed for free tier, SIP feed for paid.",
        config={"has_keys": has_alpaca},
    ))

    # 3. Polygon.io
    has_polygon = _check_env_key("POLYGON_API_KEY")
    polygon_cfg = connector_config.get("polygon", {})
    sources.append(DataSource(
        id="polygon",
        name="Polygon.io",
        type="streaming",
        connector_class="PolygonConnector",
        status=ConnectorStatus.online if has_polygon else ConnectorStatus.missing_keys,
        data_types=polygon_cfg.get("data_types", ["trade", "quote", "bar"]),
        supported_assets=["equity", "options", "crypto", "forex"],
        description="Institutional-grade market data. Real-time WebSocket streaming "
                    "for US stocks, options, crypto, and forex.",
        config={"has_keys": has_polygon},
    ))

    # 4. Interactive Brokers
    has_ib = _check_env_key("IB_GATEWAY_HOST") or _check_env_key("TWS_HOST")
    ib_cfg = connector_config.get("ib", {})
    sources.append(DataSource(
        id="ib",
        name="Interactive Brokers",
        type="hybrid",
        connector_class="IBConnector",
        status=ConnectorStatus.online if has_ib else ConnectorStatus.missing_keys,
        data_types=ib_cfg.get("data_types", ["tick", "bar", "orderbook"]),
        supported_assets=["equity", "options", "futures", "forex"],
        description="Professional-grade data via TWS/Gateway. Tick-level data, "
                    "options chains, futures, and global markets.",
        config={"has_keys": has_ib},
    ))

    # 5. FRED — Macro economic data
    has_fred = _check_env_key("FRED_API_KEY")
    sources.append(DataSource(
        id="fred",
        name="FRED (Federal Reserve)",
        type="batch",
        connector_class="FREDConnector",
        status=ConnectorStatus.online if has_fred else ConnectorStatus.missing_keys,
        data_types=["macro"],
        supported_assets=["macro"],
        description="US economic indicators: yield curves, GDP, CPI, employment, "
                    "credit spreads. Key input for macro-regime strategies.",
        config={"has_keys": has_fred},
    ))

    # 6. DEX — On-chain
    sources.append(DataSource(
        id="dex",
        name="DEX On-Chain",
        type="streaming",
        connector_class="DEXConnector",
        status=ConnectorStatus.configured,
        data_types=["swap", "liquidity", "price"],
        supported_assets=["defi"],
        description="Decentralized exchange data from on-chain sources. "
                    "Uniswap, SushiSwap, PancakeSwap liquidity and swap events.",
    ))

    # 7. News/Sentiment
    sources.append(DataSource(
        id="news",
        name="News & Sentiment",
        type="streaming",
        connector_class="NewsConnector",
        status=ConnectorStatus.configured,
        data_types=["sentiment", "event", "headline"],
        supported_assets=["equity", "crypto"],
        description="News sentiment analysis from multiple sources. "
                    "Real-time headlines, sentiment scores, event detection.",
    ))

    # 8. Yahoo Finance (fallback, always available)
    sources.append(DataSource(
        id="yfinance",
        name="Yahoo Finance",
        type="batch",
        connector_class="yfinance",
        status=ConnectorStatus.online,
        data_types=["ohlcv", "fundamental"],
        supported_assets=["equity", "crypto", "forex", "indices"],
        description="Free historical OHLCV data for global markets. "
                    "Good for backtesting. No real-time streaming.",
    ))

    return sources


# ── Data inventory ──────────────────────────────────────────────

def _scan_inventory() -> List[DatasetInfo]:
    """Scan local storage to discover available datasets."""
    config = _load_config()
    symbols_cfg = config.get("symbols", {})
    datasets: List[DatasetInfo] = []

    # Config-defined symbols (what SHOULD be collected)
    for asset_class, symbols in symbols_cfg.items():
        for symbol in symbols:
            # Check if we have YAML config for strategies using this symbol
            freq = "1h" if asset_class == "crypto" else "1d"
            datasets.append(DatasetInfo(
                symbol=symbol,
                asset_class=asset_class,
                source="ccxt" if asset_class == "crypto" else "alpaca",
                data_type="ohlcv",
                frequency=freq,
                storage_tier="warm",
                freshness="unknown",
            ))

    # Scan for actual data files in data/sample/
    if SAMPLE_DIR.exists():
        for f in SAMPLE_DIR.glob("*.csv"):
            name = f.stem
            size_mb = f.stat().st_size / (1024 * 1024)
            datasets.append(DatasetInfo(
                symbol=name.upper(),
                asset_class="sample",
                source="file",
                data_type="ohlcv",
                frequency="varies",
                storage_tier="cold",
                size_mb=round(size_mb, 2),
                freshness="static",
            ))

    # Check for parquet files
    parquet_dir = DATA_DIR / "parquet"
    if parquet_dir.exists():
        for f in parquet_dir.rglob("*.parquet"):
            size_mb = f.stat().st_size / (1024 * 1024)
            parts = f.relative_to(parquet_dir).parts
            datasets.append(DatasetInfo(
                symbol=parts[1] if len(parts) > 1 else f.stem,
                asset_class=parts[0] if parts else "unknown",
                source="parquet_store",
                data_type=parts[2] if len(parts) > 2 else "ohlcv",
                frequency="varies",
                storage_tier="cold",
                size_mb=round(size_mb, 2),
                freshness="archived",
            ))

    # Check for backtest data / logs (user dir, persistent)
    from core.paths import get_logs_dir
    log_dir = get_logs_dir() / "paper_trading"
    if log_dir.exists():
        for f in log_dir.glob("*.jsonl"):
            size_mb = f.stat().st_size / (1024 * 1024)
            line_count = sum(1 for _ in open(f)) if size_mb < 10 else None
            datasets.append(DatasetInfo(
                symbol=f.stem.replace("_daily", ""),
                asset_class="strategy_log",
                source="paper_trading",
                data_type="daily_log",
                frequency="1d",
                storage_tier="warm",
                records=line_count,
                size_mb=round(size_mb, 2),
                freshness="< 1d" if (datetime.now().timestamp() - f.stat().st_mtime) < 86400 else "stale",
            ))

    # Check live state files
    state_dir = DATA_DIR / "live_state"
    if state_dir.exists():
        for f in state_dir.glob("*.pkl"):
            size_mb = f.stat().st_size / (1024 * 1024)
            datasets.append(DatasetInfo(
                symbol=f.stem.replace("_state", ""),
                asset_class="strategy_state",
                source="live_engine",
                data_type="state_snapshot",
                frequency="realtime",
                storage_tier="hot",
                size_mb=round(size_mb, 2),
                freshness="< 1h" if (datetime.now().timestamp() - f.stat().st_mtime) < 3600 else "stale",
            ))

    return datasets


# ── Feature store ──────────────────────────────────────────────

def _get_features() -> List[FeatureInfo]:
    """List all available computed features from config."""
    config = _load_config()
    features_cfg = config.get("features", {})
    features: List[FeatureInfo] = []

    # Technical indicators
    tech = features_cfg.get("technical", {})
    ema_periods = tech.get("ema_periods", [9, 21, 55, 200])
    for p in ema_periods:
        features.append(FeatureInfo(
            name=f"EMA_{p}",
            category="technical",
            description=f"Exponential Moving Average with {p}-period lookback",
            parameters={"period": p},
            frequency="1m+",
            dependencies=["ohlcv"],
        ))

    features.append(FeatureInfo(
        name="RSI",
        category="technical",
        description="Relative Strength Index — momentum oscillator (0-100)",
        parameters={"period": tech.get("rsi_period", 14)},
        frequency="1m+",
        dependencies=["ohlcv"],
    ))

    features.append(FeatureInfo(
        name="MACD",
        category="technical",
        description="Moving Average Convergence Divergence — trend/momentum",
        parameters={
            "fast": tech.get("macd_fast", 12),
            "slow": tech.get("macd_slow", 26),
            "signal": tech.get("macd_signal", 9),
        },
        frequency="1m+",
        dependencies=["ohlcv"],
    ))

    features.append(FeatureInfo(
        name="Bollinger_Bands",
        category="technical",
        description="Bollinger Bands — volatility-based envelope around SMA",
        parameters={
            "period": tech.get("bollinger_period", 20),
            "std": tech.get("bollinger_std", 2.0),
        },
        frequency="1m+",
        dependencies=["ohlcv"],
    ))

    features.append(FeatureInfo(
        name="ATR",
        category="technical",
        description="Average True Range — volatility measure for stop-loss sizing",
        parameters={"period": tech.get("atr_period", 14)},
        frequency="1m+",
        dependencies=["ohlcv"],
    ))

    # Microstructure
    micro = features_cfg.get("microstructure", {})
    features.append(FeatureInfo(
        name="Orderbook_Imbalance",
        category="microstructure",
        description="Bid-ask depth imbalance ratio — directional pressure signal",
        parameters={"depth": micro.get("orderbook_depth", 20)},
        frequency="tick",
        dependencies=["orderbook"],
    ))

    features.append(FeatureInfo(
        name="VPIN",
        category="microstructure",
        description="Volume-Synchronized Probability of Informed Trading",
        parameters={"bucket_size": micro.get("vpin_bucket_size", 50)},
        frequency="tick",
        dependencies=["trade"],
    ))

    features.append(FeatureInfo(
        name="OFI",
        category="microstructure",
        description="Order Flow Imbalance — net buying/selling pressure",
        parameters={"window": micro.get("ofi_window", 100)},
        frequency="tick",
        dependencies=["orderbook"],
    ))

    # Macro
    features.append(FeatureInfo(
        name="Yield_Curve",
        category="macro",
        description="US Treasury yield curve slope (10Y - 2Y spread)",
        parameters={"series": ["DGS10", "DGS2"]},
        frequency="1d",
        dependencies=["fred"],
    ))

    features.append(FeatureInfo(
        name="Credit_Spread",
        category="macro",
        description="Investment-grade credit spread — risk appetite indicator",
        parameters={"series": ["BAMLC0A0CM"]},
        frequency="1d",
        dependencies=["fred"],
    ))

    features.append(FeatureInfo(
        name="VIX",
        category="macro",
        description="CBOE Volatility Index — market fear gauge",
        parameters={"symbol": "^VIX"},
        frequency="1m",
        dependencies=["ohlcv"],
    ))

    # Sentiment
    features.append(FeatureInfo(
        name="News_Sentiment",
        category="sentiment",
        description="Aggregated news sentiment score (-1 to +1) from headlines",
        parameters={"window": "24h", "sources": ["reuters", "bloomberg"]},
        frequency="1h",
        dependencies=["news"],
    ))

    return features


# ── Pipeline overview ──────────────────────────────────────────

def _get_pipelines() -> List[PipelineInfo]:
    """Describe the data pipeline architecture."""
    return [
        PipelineInfo(
            name="Crypto Realtime Stream",
            type="streaming",
            source="CCXT WebSocket (Binance, OKX, Bybit)",
            sink="Kafka → Redis (hot) + TimescaleDB (warm)",
            status="configured",
            throughput="~1000 msg/s per exchange",
            description="Real-time trades, orderbook L2, and OHLCV candles via WebSocket. "
                        "Normalized to unified schema, published to Kafka topics, "
                        "cached in Redis for sub-ms strategy access.",
        ),
        PipelineInfo(
            name="Equity Realtime Feed",
            type="streaming",
            source="Alpaca IEX/SIP WebSocket",
            sink="Kafka → Redis (hot) + TimescaleDB (warm)",
            status="online" if _check_env_key("ALPACA_API_KEY") else "missing_keys",
            throughput="~500 msg/s",
            description="US equity quotes, trades, and bars from Alpaca Markets. "
                        "Real-time streaming with automatic reconnection.",
        ),
        PipelineInfo(
            name="Historical Backfill (Batch)",
            type="batch",
            source="CCXT REST / Alpaca REST / yfinance",
            sink="TimescaleDB (warm) → Parquet (cold)",
            status="available",
            description="On-demand historical data fetch. Fetches OHLCV bars for specified "
                        "symbol/timeframe/date range. Stores in TimescaleDB for fast queries, "
                        "archives to Parquet on S3/MinIO for long-term retention.",
        ),
        PipelineInfo(
            name="Feature Computation",
            type="batch",
            source="TimescaleDB OHLCV + Redis orderbook",
            sink="TimescaleDB features table + Redis feature cache",
            status="configured",
            description="Incremental feature computation triggered on new bar arrival. "
                        "Computes technical indicators (EMA, RSI, MACD, ATR, BB), "
                        "microstructure features (VPIN, OFI, imbalance), and macro signals.",
        ),
        PipelineInfo(
            name="Macro Data Sync",
            type="batch",
            source="FRED API",
            sink="TimescaleDB → Feature Store",
            status="configured",
            throughput="Daily refresh",
            description="Daily sync of US economic indicators: yield curves, credit spreads, "
                        "employment data, GDP, CPI. Used for macro regime detection.",
        ),
        PipelineInfo(
            name="Parquet Archive",
            type="batch",
            source="TimescaleDB (aged data)",
            sink="MinIO/S3 Parquet files",
            status="configured",
            description="Automated archival of aged time-series data from TimescaleDB to "
                        "Parquet on object storage. Partitioned by asset_class/symbol/year/month. "
                        "Snappy compression, ~80% size reduction vs raw.",
        ),
    ]


# ── Collection job execution ──────────────────────────────────

async def _run_collection_job(job_id: str, req: CollectionRequest):
    """Execute a data collection job in background."""
    job = _collection_jobs[job_id]
    job["status"] = "running"

    try:
        # Try CCXT for crypto
        if req.source == "ccxt":
            try:
                import ccxt
                exchange_cls = getattr(ccxt, req.exchange, None)
                if not exchange_cls:
                    raise ValueError(f"Exchange {req.exchange} not found in CCXT")

                exchange = exchange_cls({"enableRateLimit": True})
                since = None
                if req.start_date:
                    since = exchange.parse8601(req.start_date + "T00:00:00Z")

                limit = 1000
                ohlcv = exchange.fetch_ohlcv(
                    req.symbol,
                    timeframe=req.frequency,
                    since=since,
                    limit=limit,
                )

                job["records_fetched"] = len(ohlcv)
                job["progress"] = 1.0
                job["status"] = "completed"
                job["completed_at"] = datetime.now().isoformat()

                # Store in data directory
                import json
                out_dir = DATA_DIR / "collected" / req.source
                out_dir.mkdir(parents=True, exist_ok=True)
                safe_symbol = req.symbol.replace("/", "_")
                out_file = out_dir / f"{safe_symbol}_{req.frequency}_{req.exchange}.json"
                with open(out_file, "w") as f:
                    json.dump({
                        "symbol": req.symbol,
                        "exchange": req.exchange,
                        "frequency": req.frequency,
                        "data_type": req.data_type,
                        "collected_at": datetime.now().isoformat(),
                        "records": len(ohlcv),
                        "ohlcv": ohlcv,
                    }, f)

                exchange.close()

            except ImportError:
                job["status"] = "failed"
                job["error"] = "ccxt package not installed. Run: pip install ccxt"
            except Exception as e:
                job["status"] = "failed"
                job["error"] = str(e)

        # Try yfinance for equities
        elif req.source == "yfinance":
            try:
                import yfinance as yf
                ticker = yf.Ticker(req.symbol)
                period = "1y"
                if req.start_date:
                    hist = ticker.history(start=req.start_date, end=req.end_date or None, interval=req.frequency)
                else:
                    hist = ticker.history(period=period, interval=req.frequency)

                job["records_fetched"] = len(hist)
                job["progress"] = 1.0
                job["status"] = "completed"
                job["completed_at"] = datetime.now().isoformat()

                # Store as CSV
                out_dir = DATA_DIR / "collected" / "yfinance"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_file = out_dir / f"{req.symbol}_{req.frequency}.csv"
                hist.to_csv(str(out_file))

            except ImportError:
                job["status"] = "failed"
                job["error"] = "yfinance package not installed"
            except Exception as e:
                job["status"] = "failed"
                job["error"] = str(e)

        else:
            job["status"] = "failed"
            job["error"] = f"Source '{req.source}' not supported for collection"

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)


# ── Endpoints ────────────────────────────────────────────────────

@router.get("/sources", response_model=List[DataSource])
async def get_data_sources():
    """List all configured data connectors and their status."""
    return _discover_sources()


@router.get("/inventory", response_model=List[DatasetInfo])
async def get_data_inventory():
    """List all available datasets across storage tiers."""
    return _scan_inventory()


@router.get("/features", response_model=List[FeatureInfo])
async def get_features():
    """List all available computed features in the feature store."""
    return _get_features()


@router.get("/pipeline", response_model=List[PipelineInfo])
async def get_pipeline_status():
    """Get data pipeline architecture and status."""
    return _get_pipelines()


@router.post("/collect", response_model=CollectionJob)
async def start_collection(req: CollectionRequest):
    """Start a new data collection job."""
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    now = datetime.now().isoformat()

    job_data = {
        "id": job_id,
        "symbol": req.symbol,
        "source": req.source,
        "data_type": req.data_type,
        "frequency": req.frequency,
        "start_date": req.start_date or (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
        "end_date": req.end_date or datetime.now().strftime("%Y-%m-%d"),
        "status": "pending",
        "progress": 0.0,
        "records_fetched": 0,
        "created_at": now,
        "completed_at": None,
        "error": None,
    }
    _collection_jobs[job_id] = job_data

    # Run in background
    asyncio.create_task(_run_collection_job(job_id, req))

    return CollectionJob(**job_data)


@router.get("/jobs", response_model=List[CollectionJob])
async def list_collection_jobs():
    """List all collection jobs."""
    return [CollectionJob(**j) for j in _collection_jobs.values()]


@router.get("/jobs/{job_id}", response_model=CollectionJob)
async def get_collection_job(job_id: str):
    """Get status of a collection job."""
    if job_id not in _collection_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return CollectionJob(**_collection_jobs[job_id])


@router.get("/config/symbols")
async def get_configured_symbols():
    """Get all symbols configured for tracking."""
    config = _load_config()
    return config.get("symbols", {})
