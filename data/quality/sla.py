"""Data SLA — declared service levels with a trading preflight gate.

Week-4 of the data layer upgrade. Two consumers:
  * /api/data-health/sla       — full SLA evaluation for the dashboard
  * preflight(sources)          — sync gate for strategy code: called before
                                  OPENING a position; a critical breach on a
                                  declared dependency returns (False, reason).

Doctrine (inherited from the pre-trade risk incident): the gate applies to
risk-INCREASING actions only. Settling, closing, marking — always proceed.
Breaches are recorded to the corrections ledger for audit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml

from core.logging import get_logger
from core.paths import get_resource_root
from data.quality.guards import record_correction
from data.quality.probes import PROBES

logger = get_logger(__name__)

_SLA_PATH = get_resource_root() / "config" / "data_sla.yaml"
_cfg_cache: Optional[dict] = None


def load_sla() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = yaml.safe_load(_SLA_PATH.read_text())
    return _cfg_cache


@dataclass
class SLABreach:
    target: str
    critical: bool
    detail: str


@dataclass
class SLAResult:
    ok: bool
    breaches: List[SLABreach] = field(default_factory=list)

    @property
    def critical_breaches(self) -> List[SLABreach]:
        return [b for b in self.breaches if b.critical]


def _probe_source(source_id: str, spec: dict) -> Optional[SLABreach]:
    kind_fn = PROBES.get(source_id)
    if kind_fn is None:
        return SLABreach(source_id, spec.get("critical", False),
                         "no probe defined for source")
    _, fn = kind_fn
    t0 = time.time()
    try:
        fn()
    except Exception as e:
        return SLABreach(source_id, spec.get("critical", False),
                         f"probe failed: {type(e).__name__}: {e}"[:140])
    latency = (time.time() - t0) * 1000
    max_lat = spec.get("max_latency_ms")
    if max_lat and latency > max_lat:
        return SLABreach(source_id, spec.get("critical", False),
                         f"latency {latency:.0f}ms > SLA {max_lat}ms")
    return None


def check_sources(only: Optional[List[str]] = None) -> SLAResult:
    """Evaluate source SLAs (sync; run via asyncio.to_thread from async code)."""
    cfg = load_sla().get("sources", {})
    res = SLAResult(ok=True)
    for sid, spec in cfg.items():
        if only is not None and sid not in only:
            continue
        breach = _probe_source(sid, spec)
        if breach:
            res.breaches.append(breach)
            record_correction(sid, "sla_breach", breach.detail)
    res.ok = not res.critical_breaches
    return res


def preflight(sources: List[str], strategy: str = "") -> Tuple[bool, str]:
    """Gate for risk-INCREASING actions: may we open a new position?

    Returns (allowed, reason). Only CRITICAL breaches on the declared
    dependencies block; non-critical breaches are logged and allowed.
    Fails OPEN on gate errors — a broken gate must not halt trading by
    itself (the strategy's own data fetch will fail loudly if truly down).
    """
    try:
        res = check_sources(only=sources)
        if res.ok:
            return True, "sla_ok"
        detail = "; ".join(f"{b.target}: {b.detail}"
                           for b in res.critical_breaches)
        logger.warning("sla_preflight_blocked", strategy=strategy,
                       detail=detail)
        record_correction(strategy or "preflight", "open_blocked", detail)
        return False, detail
    except Exception as e:
        logger.warning("sla_preflight_error_failing_open", error=str(e))
        return True, f"gate_error:{type(e).__name__}"
