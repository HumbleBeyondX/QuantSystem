"""Paper -> Live promotion: gate evaluation and the promotion action.

The decision system the platform was missing: a strategy leaves paper for
live ONLY through evaluate_promotion() -> all checks green -> promote_to_live().

Design:
  * Gates declared in config/promotion_gates.yaml (exit criteria for paper).
  * Evidence comes from the strategy's own paper telemetry
    (logs/paper_trading/{id}_daily.jsonl + {id}_trades.jsonl) and the live
    SLA state of its declared sources.
  * Promotion NEVER edits config/strategy_registry.yaml (hand-maintained).
    It writes a stage override to data/stage_overrides.json which the
    registry loader applies, plus an append-only audit row in
    data/promotions.jsonl and a corrections-ledger entry.
  * force=True requires a reason and is recorded as such — the audit trail
    never launders a forced promotion into a clean one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.logging import get_logger
from core.paths import get_data_dir, get_logs_dir, get_resource_root

logger = get_logger(__name__)

_GATES_PATH = get_resource_root() / "config" / "promotion_gates.yaml"
OVERRIDES_PATH = get_data_dir() / "stage_overrides.json"
AUDIT_PATH = get_data_dir() / "promotions.jsonl"


@dataclass
class Check:
    id: str
    name: str
    passed: Optional[bool]      # None = not evaluable yet
    detail: str


@dataclass
class PromotionReport:
    strategy_id: str
    ready: bool
    checks: List[Check] = field(default_factory=list)
    throttle: float = 0.10
    evaluated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self),
                "checks": [asdict(c) for c in self.checks]}


def _load_gates(strategy_id: str) -> Dict[str, Any]:
    raw = yaml.safe_load(_GATES_PATH.read_text(encoding="utf-8"))
    cfg = dict(raw.get("default", {}))
    cfg.update(raw.get("strategies", {}).get(strategy_id, {}))
    return cfg


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _paper_evidence(strategy_id: str) -> Dict[str, Any]:
    log_dir = get_logs_dir() / "paper_trading"
    daily = _read_jsonl(log_dir / f"{strategy_id}_daily.jsonl")
    trades = _read_jsonl(log_dir / f"{strategy_id}_trades.jsonl")
    ev: Dict[str, Any] = {"daily_rows": len(daily)}
    if daily:
        first = daily[0].get("date") or daily[0].get("logged_at", "")[:10]
        last = daily[-1].get("date") or daily[-1].get("logged_at", "")[:10]
        try:
            ev["paper_days"] = (datetime.fromisoformat(last)
                                - datetime.fromisoformat(first)).days
        except ValueError:
            ev["paper_days"] = None
        eq = [r.get("total_equity") for r in daily
              if isinstance(r.get("total_equity"), (int, float))]
        if len(eq) >= 2:
            peak, mdd = eq[0], 0.0
            for v in eq:
                peak = max(peak, v)
                mdd = min(mdd, v / peak - 1)
            ev["max_drawdown"] = round(mdd, 4)
    pnls = [t.get("pnl") for t in trades
            if t.get("action") in ("SETTLE", "CLOSE", "CLOSE_LONG", "CLOSE_SHORT")
            and isinstance(t.get("pnl"), (int, float))]
    ev["closed_trades"] = len(pnls)
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p <= 0]
        wr = len(wins) / len(pnls)
        pf = (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else None
        ev["win_rate"] = round(wr, 3)
        ev["profit_factor"] = round(pf, 2) if pf is not None else None
        ev["expectancy"] = (round(wr * pf - (1 - wr), 3)
                            if pf is not None else None)
    return ev


def evaluate_promotion(strategy_id: str) -> PromotionReport:
    """Run every gate; never raises — a broken check reports itself."""
    cfg = _load_gates(strategy_id)
    rep = PromotionReport(
        strategy_id=strategy_id, ready=False,
        throttle=float(cfg.get("live_notional_throttle", 0.10)),
        evaluated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    ev = _paper_evidence(strategy_id)

    # 1. paper duration
    need_d = cfg.get("min_paper_days", 56)
    days = ev.get("paper_days")
    rep.checks.append(Check(
        "paper_days", f"Paper duration ≥ {need_d}d",
        None if days is None else days >= need_d,
        f"{days if days is not None else 'no paper log'} days on record"))

    # 2. closed trades
    need_t = cfg.get("min_closed_trades", 6)
    n = ev.get("closed_trades", 0)
    rep.checks.append(Check(
        "closed_trades", f"Closed trades ≥ {need_t}",
        n >= need_t, f"{n} settlements/closes recorded"))

    # 3. expectancy
    need_e = cfg.get("min_expectancy", 0.03)
    exp = ev.get("expectancy")
    rep.checks.append(Check(
        "expectancy", f"Expectancy ≥ {need_e}",
        None if exp is None else exp >= need_e,
        (f"expectancy={exp} (win_rate={ev.get('win_rate')}, "
         f"PF={ev.get('profit_factor')})") if exp is not None
        else "not enough closed trades to compute"))

    # 4. paper drawdown
    need_dd = cfg.get("max_paper_drawdown", 0.15)
    mdd = ev.get("max_drawdown")
    rep.checks.append(Check(
        "drawdown", f"Paper max drawdown ≤ {need_dd:.0%}",
        None if mdd is None else abs(mdd) <= need_dd,
        f"paper max DD = {mdd:.1%}" if mdd is not None else "no equity history"))

    # 5. SLA on declared sources (live check, right now)
    if cfg.get("require_sla_green", True):
        try:
            from data.quality.sla import check_sources
            sources = cfg.get("sources", [])
            res = check_sources(only=sources) if sources else None
            ok = bool(res.ok) if res is not None else None
            detail = ("all green" if ok else
                      "; ".join(f"{b.target}: {b.detail}" for b in res.breaches)
                      ) if res is not None else "no sources declared"
            rep.checks.append(Check(
                "sla", f"Data SLA green ({', '.join(sources) or '—'})", ok, detail))
        except Exception as e:
            rep.checks.append(Check("sla", "Data SLA green", None,
                                    f"gate error: {type(e).__name__}"))

    # 6. backtest gate from the registry (entry gate must have been honest)
    if cfg.get("require_backtest_gate", True):
        try:
            from dashboard.backend.routers.strategies import CATALOG_MAP
            s = CATALOG_MAP.get(strategy_id)
            if s is None:
                rep.checks.append(Check("backtest_gate", "Registry gate passed",
                                        False, "strategy not in registry"))
            elif s.gate.get("passed") is True:
                rep.checks.append(Check(
                    "backtest_gate", "Registry gate passed", True,
                    f"OOS Sharpe {s.gate.get('oos_sharpe')} ≥ "
                    f"{s.gate.get('threshold', 1.5)}"))
            else:
                # combo-leg approval: linked record must be validated
                rec = s.links.get("record")
                combo_ok = False
                if rec:
                    combo = CATALOG_MAP.get(rec)
                    combo_ok = bool(combo and combo.gate.get("passed"))
                rep.checks.append(Check(
                    "backtest_gate",
                    "Registry gate passed (or validated combo leg)", combo_ok,
                    (f"standalone gate failed but linked record '{rec}' is a "
                     f"validated combo — approved as leg") if combo_ok else
                    "standalone gate failed and no validated combo link"))
        except Exception as e:
            rep.checks.append(Check("backtest_gate", "Registry gate passed",
                                    None, f"gate error: {type(e).__name__}"))

    # 7+8. post-incident doctrine: validation suite + cross-engine
    if cfg.get("require_validation_suite", True) or cfg.get("require_cross_engine", True):
        record = _load_linked_record(strategy_id)
        if cfg.get("require_validation_suite", True):
            suite = (record or {}).get("validation_suite") or {}
            passed = suite.get("all_passed")
            rep.checks.append(Check(
                "validation_suite",
                "Robustness suite passed (fee stress / WF / LOO)",
                bool(passed) if passed is not None else None,
                (f"all checks green: {suite.get('checks')}" if passed
                 else "no validation_suite in linked record — run "
                      "backtesting.validation.validation_report and attach")))
        if cfg.get("require_cross_engine", True):
            ce = (record or {}).get("cross_engine") or {}
            caveats = " ".join((record or {}).get("caveats", []))
            waived = ("cross-check infeasible" in caveats.lower()
                      or ce.get("waiver"))
            if ce.get("confirmed") is True:
                rep.checks.append(Check(
                    "cross_engine", "Cross-engine confirmation",
                    True, f"confirmed via {ce.get('engine', 'independent engine')}"))
            elif waived:
                rep.checks.append(Check(
                    "cross_engine", "Cross-engine confirmation",
                    True, "WAIVED with recorded reason (options: no historical "
                          "chains; real-chain paper is the arbiter)"))
            else:
                rep.checks.append(Check(
                    "cross_engine", "Cross-engine confirmation",
                    False, "no confirmation and no recorded waiver — port to "
                           "the Nautilus harness (scripts/nautilus_pilot)"))

    rep.ready = all(c.passed is True for c in rep.checks)
    return rep


def _load_linked_record(strategy_id: str) -> Optional[Dict[str, Any]]:
    """The official research record the registry links for this strategy."""
    try:
        from dashboard.backend.routers.strategies import CATALOG_MAP
        s = CATALOG_MAP.get(strategy_id)
        rid = s.links.get("record") if s else None
        if not rid:
            return None
        path = get_resource_root() / "results" / f"{rid}_research.json"
        if not path.exists():
            path = get_resource_root() / "results" / f"{rid}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


# ── stage overrides + promotion action ────────────────────────────

def load_stage_overrides() -> Dict[str, Dict[str, Any]]:
    if OVERRIDES_PATH.exists():
        try:
            return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def promote_to_live(strategy_id: str, force: bool = False,
                    reason: str = "") -> Dict[str, Any]:
    """Execute the promotion. Raises ValueError when gates fail without force."""
    rep = evaluate_promotion(strategy_id)
    if not rep.ready and not force:
        failing = [c.id for c in rep.checks if c.passed is not True]
        raise ValueError(f"gates not passed: {failing}")
    if force and not reason.strip():
        raise ValueError("force promotion requires a reason")

    overrides = load_stage_overrides()
    overrides[strategy_id] = {
        "stage": "live",
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "forced": bool(force and not rep.ready),
        "reason": reason,
        "throttle": rep.throttle,
    }
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH.write_text(json.dumps(overrides, indent=1), encoding="utf-8")

    audit = {"ts": overrides[strategy_id]["promoted_at"],
             "strategy": strategy_id, "action": "promote_live",
             "forced": overrides[strategy_id]["forced"], "reason": reason,
             "checks": [asdict(c) for c in rep.checks]}
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(audit) + "\n")
    try:
        from data.quality.guards import record_correction
        record_correction(strategy_id, "promotion",
                          f"paper->live (forced={audit['forced']}) {reason}"[:140])
    except Exception:
        pass
    logger.info("strategy_promoted_live", strategy=strategy_id,
                forced=audit["forced"])
    return {"strategy_id": strategy_id, "stage": "live",
            "forced": audit["forced"], "throttle": rep.throttle,
            "report": rep.to_dict()}


def demote_to_paper(strategy_id: str, reason: str) -> Dict[str, Any]:
    """Roll a live promotion back to paper (always allowed — risk-reducing)."""
    overrides = load_stage_overrides()
    overrides[strategy_id] = {
        "stage": "paper",
        "demoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
    }
    OVERRIDES_PATH.write_text(json.dumps(overrides, indent=1), encoding="utf-8")
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": overrides[strategy_id]["demoted_at"],
                            "strategy": strategy_id, "action": "demote_paper",
                            "reason": reason}) + "\n")
    return {"strategy_id": strategy_id, "stage": "paper"}
