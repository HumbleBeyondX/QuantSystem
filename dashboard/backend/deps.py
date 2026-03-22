"""FastAPI dependency injection: shared service instances."""

from __future__ import annotations

from typing import Optional

from core.events import EventBus
from data.features.computed import create_default_feature_store
from data.features.feature_store import FeatureStore
from execution.engine.paper_engine import PaperEngine
from portfolio.position.position_manager import PositionManager
from portfolio.risk.risk_manager import RiskManager

# ── Singleton instances (initialized at app startup) ────────

_event_bus: Optional[EventBus] = None
_feature_store: Optional[FeatureStore] = None
_position_manager: Optional[PositionManager] = None
_risk_manager: Optional[RiskManager] = None
_paper_engine: Optional[PaperEngine] = None


def init_services() -> None:
    """Initialize shared service instances. Called during app lifespan."""
    global _event_bus, _feature_store, _position_manager, _risk_manager, _paper_engine

    _event_bus = EventBus()
    _feature_store = create_default_feature_store()
    _position_manager = PositionManager(initial_cash=100_000.0)
    _risk_manager = RiskManager()
    _paper_engine = PaperEngine()


def get_event_bus() -> EventBus:
    assert _event_bus is not None, "Services not initialized"
    return _event_bus


def get_feature_store() -> FeatureStore:
    assert _feature_store is not None, "Services not initialized"
    return _feature_store


def get_position_manager() -> PositionManager:
    assert _position_manager is not None, "Services not initialized"
    return _position_manager


def get_risk_manager() -> RiskManager:
    assert _risk_manager is not None, "Services not initialized"
    return _risk_manager


def get_paper_engine() -> PaperEngine:
    assert _paper_engine is not None, "Services not initialized"
    return _paper_engine
