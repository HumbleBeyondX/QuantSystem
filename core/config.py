"""Configuration management using Pydantic Settings.

Loads configuration from:
1. config/settings.yaml (base config)
2. Environment variables (overrides, including .env file)
3. Strategy-specific YAML files

Environment variables take precedence over YAML values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


def _load_yaml(path: str | Path) -> Dict[str, Any]:
    """Load a YAML file, returning empty dict if not found."""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ============================================================
# Nested Config Models
# ============================================================


class TimescaleDBConfig(BaseSettings):
    host: str = Field("localhost", alias="TSDB_HOST")
    port: int = Field(5432, alias="TSDB_PORT")
    user: str = Field("quant", alias="TSDB_USER")
    password: str = Field("quantpass", alias="TSDB_PASSWORD")
    database: str = Field("quantdb", alias="TSDB_DB")
    pool_min: int = 2
    pool_max: int = 10

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


class RedisConfig(BaseSettings):
    url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")
    max_connections: int = 20


class KafkaConfig(BaseSettings):
    bootstrap_servers: str = Field("localhost:19092", alias="KAFKA_BOOTSTRAP_SERVERS")
    consumer_group: str = "quant-system"
    auto_offset_reset: str = "latest"
    batch_size: int = 100
    linger_ms: int = 10


class MinIOConfig(BaseSettings):
    endpoint: str = Field("localhost:9000", alias="MINIO_ENDPOINT")
    access_key: str = Field("minioadmin", alias="MINIO_ACCESS_KEY")
    secret_key: str = Field("minioadmin", alias="MINIO_SECRET_KEY")
    secure: bool = False


class IBConfig(BaseSettings):
    host: str = Field("127.0.0.1", alias="IB_HOST")
    port: int = Field(7497, alias="IB_PORT")
    client_id: int = Field(1, alias="IB_CLIENT_ID")
    timeout: int = 30
    readonly: bool = False


class PolygonConfig(BaseSettings):
    api_key: str = Field("", alias="POLYGON_API_KEY")


class TelegramConfig(BaseSettings):
    bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    chat_id: str = Field("", alias="TELEGRAM_CHAT_ID")


# ============================================================
# Master Settings
# ============================================================


class Settings(BaseSettings):
    """Master configuration object.

    Usage:
        from core.config import settings
        print(settings.kafka.bootstrap_servers)
        print(settings.timescaledb.dsn)
    """

    environment: str = "development"

    # Infrastructure
    timescaledb: TimescaleDBConfig = TimescaleDBConfig()
    redis: RedisConfig = RedisConfig()
    kafka: KafkaConfig = KafkaConfig()
    minio: MinIOConfig = MinIOConfig()

    # Brokers
    ib: IBConfig = IBConfig()
    polygon: PolygonConfig = PolygonConfig()

    # Alerts
    telegram: TelegramConfig = TelegramConfig()

    # Symbols
    symbols: Dict[str, List[str]] = {
        "crypto": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "equity": ["AAPL", "MSFT", "SPY", "QQQ"],
        "options": ["SPY", "QQQ", "AAPL"],
    }

    # Feature config
    features: Dict[str, Any] = {}

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @classmethod
    def load(cls, config_dir: str = "config") -> "Settings":
        """Load settings from YAML files and environment variables."""
        base_config = _load_yaml(Path(config_dir) / "settings.yaml")

        # Flatten nested YAML into constructor kwargs
        kwargs: Dict[str, Any] = {}
        kwargs["environment"] = base_config.get("environment", "development")
        kwargs["symbols"] = base_config.get("symbols", {})
        kwargs["features"] = base_config.get("features", {})

        # DB/infra configs merge from YAML but env vars take priority (via Pydantic)
        db_config = base_config.get("database", {})
        if "timescaledb" in db_config:
            kwargs["timescaledb"] = TimescaleDBConfig(**db_config["timescaledb"])
        if "redis" in db_config:
            kwargs["redis"] = RedisConfig(**db_config["redis"])

        kafka_config = base_config.get("kafka", {})
        if kafka_config:
            kwargs["kafka"] = KafkaConfig(**kafka_config)

        storage_config = base_config.get("storage", {})
        if "minio" in storage_config:
            kwargs["minio"] = MinIOConfig(**storage_config["minio"])

        return cls(**kwargs)


def load_strategy_config(strategy_name: str, config_dir: str = "config") -> Dict[str, Any]:
    """Load a strategy-specific YAML configuration."""
    path = Path(config_dir) / "strategies" / f"{strategy_name}.yaml"
    return _load_yaml(path)


# Singleton instance - import this everywhere
settings = Settings.load()
