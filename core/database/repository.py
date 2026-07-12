"""Repository layer — typed data-access helpers for each domain entity.

Each repository operates on a single table and exposes high-level queries
that the orchestrator, dashboard, and sync layers consume.

All public methods accept an ``AsyncSession`` — the caller owns the
transaction boundary (commit / rollback).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import select, update, delete, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.models import (
    AuditLogRecord,
    BacktestResultRecord,
    FactorEvaluationRecord,
    FillRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
    PositionSnapshot,
    RiskEventRecord,
    StrategyStateRecord,
    SyncCursor,
)

logger = logging.getLogger(__name__)


# ====================================================================
# Orders
# ====================================================================

class OrderRepo:
    """Data-access for the ``orders`` table."""

    @staticmethod
    async def insert(session: AsyncSession, **kwargs: Any) -> OrderRecord:
        rec = OrderRecord(**kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def get_by_order_id(session: AsyncSession, order_id: str) -> Optional[OrderRecord]:
        stmt = select(OrderRecord).where(OrderRecord.order_id == order_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def update_status(
        session: AsyncSession,
        order_id: str,
        *,
        status: str,
        filled_quantity: Optional[float] = None,
        average_fill_price: Optional[float] = None,
        broker_order_id: Optional[str] = None,
    ) -> None:
        values: Dict[str, Any] = {"status": status, "updated_at": datetime.now(timezone.utc)}
        if filled_quantity is not None:
            values["filled_quantity"] = filled_quantity
        if average_fill_price is not None:
            values["average_fill_price"] = average_fill_price
        if broker_order_id is not None:
            values["broker_order_id"] = broker_order_id
        stmt = update(OrderRecord).where(OrderRecord.order_id == order_id).values(**values)
        await session.execute(stmt)

    @staticmethod
    async def list_by_strategy(
        session: AsyncSession,
        strategy_id: str,
        *,
        limit: int = 100,
        status: Optional[str] = None,
    ) -> Sequence[OrderRecord]:
        stmt = select(OrderRecord).where(OrderRecord.strategy_id == strategy_id)
        if status:
            stmt = stmt.where(OrderRecord.status == status)
        stmt = stmt.order_by(desc(OrderRecord.created_at)).limit(limit)
        return (await session.execute(stmt)).scalars().all()

    @staticmethod
    async def list_recent(session: AsyncSession, *, limit: int = 50) -> Sequence[OrderRecord]:
        stmt = select(OrderRecord).order_by(desc(OrderRecord.created_at)).limit(limit)
        return (await session.execute(stmt)).scalars().all()


# ====================================================================
# Fills
# ====================================================================

class FillRepo:
    """Data-access for the ``fills`` table."""

    @staticmethod
    async def insert(session: AsyncSession, **kwargs: Any) -> FillRecord:
        rec = FillRecord(**kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def list_by_order(session: AsyncSession, order_id: str) -> Sequence[FillRecord]:
        stmt = select(FillRecord).where(FillRecord.order_id == order_id).order_by(FillRecord.timestamp)
        return (await session.execute(stmt)).scalars().all()

    @staticmethod
    async def list_by_strategy(
        session: AsyncSession,
        strategy_id: str,
        *,
        limit: int = 200,
        since: Optional[datetime] = None,
    ) -> Sequence[FillRecord]:
        stmt = select(FillRecord).where(FillRecord.strategy_id == strategy_id)
        if since:
            stmt = stmt.where(FillRecord.timestamp >= since)
        stmt = stmt.order_by(desc(FillRecord.timestamp)).limit(limit)
        return (await session.execute(stmt)).scalars().all()

    @staticmethod
    async def count_by_strategy(session: AsyncSession, strategy_id: str) -> int:
        stmt = select(func.count()).select_from(FillRecord).where(FillRecord.strategy_id == strategy_id)
        return (await session.execute(stmt)).scalar_one()


# ====================================================================
# Position Snapshots
# ====================================================================

class PositionRepo:
    """Data-access for the ``position_snapshots`` table."""

    @staticmethod
    async def insert(session: AsyncSession, **kwargs: Any) -> PositionSnapshot:
        rec = PositionSnapshot(**kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def latest_by_strategy(
        session: AsyncSession,
        strategy_id: str,
    ) -> Sequence[PositionSnapshot]:
        """Return the most recent snapshot for each symbol in a strategy."""
        subq = (
            select(
                PositionSnapshot.symbol,
                func.max(PositionSnapshot.id).label("max_id"),
            )
            .where(PositionSnapshot.strategy_id == strategy_id)
            .group_by(PositionSnapshot.symbol)
            .subquery()
        )
        stmt = select(PositionSnapshot).join(
            subq, PositionSnapshot.id == subq.c.max_id
        )
        return (await session.execute(stmt)).scalars().all()


# ====================================================================
# Portfolio Snapshots
# ====================================================================

class PortfolioRepo:
    """Data-access for the ``portfolio_snapshots`` table."""

    @staticmethod
    async def insert(session: AsyncSession, **kwargs: Any) -> PortfolioSnapshotRecord:
        rec = PortfolioSnapshotRecord(**kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def latest(session: AsyncSession) -> Optional[PortfolioSnapshotRecord]:
        stmt = select(PortfolioSnapshotRecord).order_by(desc(PortfolioSnapshotRecord.timestamp)).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def history(
        session: AsyncSession,
        *,
        limit: int = 500,
        since: Optional[datetime] = None,
    ) -> Sequence[PortfolioSnapshotRecord]:
        stmt = select(PortfolioSnapshotRecord)
        if since:
            stmt = stmt.where(PortfolioSnapshotRecord.timestamp >= since)
        stmt = stmt.order_by(desc(PortfolioSnapshotRecord.timestamp)).limit(limit)
        return (await session.execute(stmt)).scalars().all()


# ====================================================================
# Strategy State
# ====================================================================

class StrategyStateRepo:
    """Data-access for the ``strategy_state`` table."""

    @staticmethod
    async def upsert(session: AsyncSession, strategy_id: str, **kwargs: Any) -> StrategyStateRecord:
        existing = await StrategyStateRepo.get(session, strategy_id)
        if existing:
            for k, v in kwargs.items():
                setattr(existing, k, v)
            existing.updated_at = datetime.now(timezone.utc)
            await session.flush()
            return existing
        rec = StrategyStateRecord(strategy_id=strategy_id, **kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def get(session: AsyncSession, strategy_id: str) -> Optional[StrategyStateRecord]:
        stmt = select(StrategyStateRecord).where(StrategyStateRecord.strategy_id == strategy_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_all(session: AsyncSession) -> Sequence[StrategyStateRecord]:
        stmt = select(StrategyStateRecord).order_by(StrategyStateRecord.strategy_id)
        return (await session.execute(stmt)).scalars().all()

    @staticmethod
    async def update_heartbeat(session: AsyncSession, strategy_id: str) -> None:
        stmt = (
            update(StrategyStateRecord)
            .where(StrategyStateRecord.strategy_id == strategy_id)
            .values(last_heartbeat=datetime.now(timezone.utc))
        )
        await session.execute(stmt)

    @staticmethod
    async def set_state(session: AsyncSession, strategy_id: str, state: str, error: Optional[str] = None) -> None:
        values: Dict[str, Any] = {
            "state": state,
            "updated_at": datetime.now(timezone.utc),
        }
        if error is not None:
            values["error_message"] = error
        if state == "RUNNING":
            values["started_at"] = datetime.now(timezone.utc)
        elif state in ("STOPPED", "ERROR"):
            values["stopped_at"] = datetime.now(timezone.utc)
        stmt = update(StrategyStateRecord).where(StrategyStateRecord.strategy_id == strategy_id).values(**values)
        await session.execute(stmt)


# ====================================================================
# Risk Events
# ====================================================================

class RiskEventRepo:
    """Data-access for the ``risk_events`` table."""

    @staticmethod
    async def insert(session: AsyncSession, **kwargs: Any) -> RiskEventRecord:
        rec = RiskEventRecord(**kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def list_recent(
        session: AsyncSession,
        *,
        limit: int = 50,
        unacknowledged_only: bool = False,
    ) -> Sequence[RiskEventRecord]:
        stmt = select(RiskEventRecord)
        if unacknowledged_only:
            stmt = stmt.where(RiskEventRecord.acknowledged == False)
        stmt = stmt.order_by(desc(RiskEventRecord.timestamp)).limit(limit)
        return (await session.execute(stmt)).scalars().all()

    @staticmethod
    async def acknowledge(session: AsyncSession, event_id: str) -> None:
        stmt = (
            update(RiskEventRecord)
            .where(RiskEventRecord.event_id == event_id)
            .values(acknowledged=True)
        )
        await session.execute(stmt)


# ====================================================================
# Audit Log
# ====================================================================

class AuditRepo:
    """Data-access for the ``audit_log`` table."""

    @staticmethod
    async def log(
        session: AsyncSession,
        action: str,
        entity_type: str,
        entity_id: Optional[str] = None,
        user: str = "system",
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditLogRecord:
        rec = AuditLogRecord(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            user=user,
            details_json=json.dumps(details) if details else None,
        )
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def list_recent(session: AsyncSession, *, limit: int = 100) -> Sequence[AuditLogRecord]:
        stmt = select(AuditLogRecord).order_by(desc(AuditLogRecord.timestamp)).limit(limit)
        return (await session.execute(stmt)).scalars().all()


# ====================================================================
# Backtest Results
# ====================================================================

class BacktestRepo:
    """Data-access for the ``backtest_results`` table."""

    @staticmethod
    async def insert(session: AsyncSession, **kwargs: Any) -> BacktestResultRecord:
        rec = BacktestResultRecord(**kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def get(session: AsyncSession, backtest_id: str) -> Optional[BacktestResultRecord]:
        stmt = select(BacktestResultRecord).where(BacktestResultRecord.backtest_id == backtest_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_all(session: AsyncSession, *, limit: int = 50) -> Sequence[BacktestResultRecord]:
        stmt = select(BacktestResultRecord).order_by(desc(BacktestResultRecord.created_at)).limit(limit)
        return (await session.execute(stmt)).scalars().all()


# ====================================================================
# Factor Evaluations
# ====================================================================

class FactorEvaluationRepo:
    """Data-access for the ``factor_evaluations`` table."""

    @staticmethod
    async def insert(session: AsyncSession, **kwargs: Any) -> FactorEvaluationRecord:
        rec = FactorEvaluationRecord(**kwargs)
        session.add(rec)
        await session.flush()
        return rec

    @staticmethod
    async def get(session: AsyncSession, eval_id: str) -> Optional[FactorEvaluationRecord]:
        stmt = select(FactorEvaluationRecord).where(FactorEvaluationRecord.eval_id == eval_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_all(
        session: AsyncSession,
        *,
        factor_name: Optional[str] = None,
        category: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 500,
    ) -> Sequence[FactorEvaluationRecord]:
        stmt = select(FactorEvaluationRecord)
        if factor_name:
            stmt = stmt.where(FactorEvaluationRecord.factor_name == factor_name)
        if category:
            stmt = stmt.where(FactorEvaluationRecord.category == category)
        if symbol:
            stmt = stmt.where(FactorEvaluationRecord.symbol == symbol)
        stmt = stmt.order_by(desc(FactorEvaluationRecord.evaluated_at)).limit(limit)
        return (await session.execute(stmt)).scalars().all()

    @staticmethod
    async def latest_by_factor(
        session: AsyncSession,
        symbol: str,
        timeframe: str,
    ) -> List[FactorEvaluationRecord]:
        """Get the most recent evaluation for each distinct factor_name."""
        # Get all records, then dedupe in Python — simpler than window queries
        stmt = (
            select(FactorEvaluationRecord)
            .where(
                FactorEvaluationRecord.symbol == symbol,
                FactorEvaluationRecord.timeframe == timeframe,
            )
            .order_by(desc(FactorEvaluationRecord.evaluated_at))
        )
        rows = (await session.execute(stmt)).scalars().all()

        # Keep first occurrence of each (factor_name, horizon_bars)
        seen: set = set()
        latest: List[FactorEvaluationRecord] = []
        for r in rows:
            key = (r.factor_name, r.horizon_bars)
            if key not in seen:
                seen.add(key)
                latest.append(r)
        return latest


# ====================================================================
# Sync Cursors
# ====================================================================

class SyncCursorRepo:
    """Data-access for the ``sync_cursors`` table (used by SyncManager)."""

    @staticmethod
    async def get(session: AsyncSession, table_name: str) -> Optional[SyncCursor]:
        stmt = select(SyncCursor).where(SyncCursor.table_name == table_name)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def upsert(
        session: AsyncSession,
        table_name: str,
        last_synced_at: datetime,
        row_count: int = 0,
        direction: str = "up",
    ) -> SyncCursor:
        existing = await SyncCursorRepo.get(session, table_name)
        if existing:
            existing.last_synced_at = last_synced_at
            existing.row_count = row_count
            existing.direction = direction
            await session.flush()
            return existing
        rec = SyncCursor(
            table_name=table_name,
            last_synced_at=last_synced_at,
            row_count=row_count,
            direction=direction,
        )
        session.add(rec)
        await session.flush()
        return rec
