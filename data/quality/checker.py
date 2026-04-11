"""Data Quality Checker for market data.

Validates bar data before it enters strategy pipelines.
Catches NaN, missing fields, stale data, and impossible values early
so the dashboard stays healthy even when data sources fail.

Usage:
    from data.quality import DataQualityChecker, DataQualityError

    dqc = DataQualityChecker()

    # Single bar check (raises DataQualityError if critical)
    report = dqc.check_bar(bar_data)

    # Historical bars check
    report = dqc.check_bars(bars_list)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from core.logging import get_logger

logger = get_logger(__name__)


class Severity(Enum):
    """Quality issue severity."""
    INFO = "info"          # Minor, data still usable
    WARNING = "warning"    # Degraded quality, defaults applied
    CRITICAL = "critical"  # Data unusable, must stop


@dataclass
class QualityIssue:
    """A single data quality issue."""
    field: str
    severity: Severity
    message: str
    value: Any = None
    fixed_value: Any = None  # If auto-fixed, what it was replaced with


@dataclass
class QualityReport:
    """Result of a data quality check."""
    passed: bool = True
    issues: List[QualityIssue] = field(default_factory=list)
    bar_data: Optional[Dict[str, Any]] = None  # Cleaned bar data (None if failed)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def has_critical(self) -> bool:
        return any(i.severity == Severity.CRITICAL for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == Severity.WARNING for i in self.issues)

    @property
    def summary(self) -> str:
        if not self.issues:
            return "All checks passed"
        counts = {}
        for i in self.issues:
            counts[i.severity.value] = counts.get(i.severity.value, 0) + 1
        parts = [f"{v} {k}" for k, v in counts.items()]
        return f"Issues: {', '.join(parts)}"


class DataQualityError(Exception):
    """Raised when data quality check fails with critical issues."""

    def __init__(self, report: QualityReport):
        self.report = report
        issues_str = "; ".join(
            f"[{i.severity.value}] {i.field}: {i.message}" for i in report.issues
        )
        super().__init__(f"Data quality check failed: {issues_str}")


# ── Validation constants ──────────────────────────────────────────

# SPY reasonable price range (adjustable)
SPY_PRICE_MIN = 50.0
SPY_PRICE_MAX = 2000.0

# VIX reasonable range
VIX_MIN = 5.0
VIX_MAX = 100.0

# Implied vol range (as decimal)
IV_MIN = 0.01
IV_MAX = 2.0

# Risk-free rate range
RF_MIN = 0.0
RF_MAX = 0.20

# Volume sanity
VOLUME_MIN = 0

# Maximum data staleness (hours)
MAX_STALENESS_HOURS = 48

# Required fields for a valid bar
REQUIRED_FIELDS = ["open", "high", "low", "close", "volume"]
METADATA_FIELDS = ["implied_vol", "risk_free_rate", "vix_close"]


class DataQualityChecker:
    """Validates market data bars before they enter the strategy pipeline.

    Design principles:
    - CRITICAL issues → raise DataQualityError, stop the run
    - WARNING issues → log + auto-fix with defaults, continue
    - INFO issues → log only
    - Dashboard always stays functional (errors are isolated to trading)
    """

    def __init__(
        self,
        spy_price_range: tuple = (SPY_PRICE_MIN, SPY_PRICE_MAX),
        vix_range: tuple = (VIX_MIN, VIX_MAX),
        iv_range: tuple = (IV_MIN, IV_MAX),
        rf_range: tuple = (RF_MIN, RF_MAX),
        max_staleness_hours: int = MAX_STALENESS_HOURS,
        strict: bool = False,
    ):
        self.spy_price_range = spy_price_range
        self.vix_range = vix_range
        self.iv_range = iv_range
        self.rf_range = rf_range
        self.max_staleness_hours = max_staleness_hours
        self.strict = strict  # In strict mode, warnings become critical

        # Track recent bars for staleness/gap detection
        self._last_bar_time: Optional[datetime] = None
        self._recent_closes: List[float] = []

    def check_bar(self, bar_data: Dict[str, Any]) -> QualityReport:
        """Validate a single bar. Raises DataQualityError on critical failure.

        Returns a QualityReport with cleaned bar_data (auto-fixed where possible).
        """
        report = QualityReport()
        issues = report.issues

        if bar_data is None:
            issues.append(QualityIssue(
                field="bar_data", severity=Severity.CRITICAL,
                message="Bar data is None (no data received from source)",
            ))
            report.passed = False
            self._finalize_report(report)
            return report

        # Work on a copy so we don't mutate the original
        cleaned = dict(bar_data)

        # 1. Check required OHLCV fields exist
        self._check_required_fields(cleaned, issues)

        # 2. Check for NaN / None in numeric fields
        self._check_nan_values(cleaned, issues)

        # 3. Check OHLC price sanity
        self._check_price_sanity(cleaned, issues)

        # 4. Check OHLC relationship (high >= low, etc.)
        self._check_ohlc_consistency(cleaned, issues)

        # 5. Check metadata (IV, VIX, risk-free rate)
        self._check_metadata(cleaned, issues)

        # 6. Check timestamp / staleness
        self._check_timestamp(cleaned, issues)

        # 7. Check for extreme moves (> 15% daily)
        self._check_extreme_moves(cleaned, issues)

        # Set result
        if report.has_critical:
            report.passed = False
            report.bar_data = None
        else:
            report.passed = True
            report.bar_data = cleaned

        self._finalize_report(report)
        return report

    def check_bars(self, bars: List[Dict[str, Any]]) -> QualityReport:
        """Validate a list of historical bars.

        Returns a combined report. Filters out bad bars but doesn't raise
        unless the entire dataset is unusable.
        """
        report = QualityReport()
        cleaned_bars = []

        if not bars:
            report.issues.append(QualityIssue(
                field="bars", severity=Severity.CRITICAL,
                message="Empty bars list (no historical data received)",
            ))
            report.passed = False
            self._finalize_report(report)
            return report

        bad_count = 0
        for i, bar in enumerate(bars):
            try:
                bar_report = self.check_bar(bar)
                if bar_report.passed and bar_report.bar_data:
                    cleaned_bars.append(bar_report.bar_data)
                else:
                    bad_count += 1
                # Collect warnings (not critical per-bar)
                for issue in bar_report.issues:
                    if issue.severity == Severity.WARNING:
                        report.issues.append(QualityIssue(
                            field=f"bar[{i}].{issue.field}",
                            severity=Severity.WARNING,
                            message=issue.message,
                            value=issue.value,
                            fixed_value=issue.fixed_value,
                        ))
            except DataQualityError:
                bad_count += 1

        # If more than 50% of bars are bad, fail
        bad_pct = bad_count / len(bars) * 100
        if bad_pct > 50:
            report.issues.append(QualityIssue(
                field="bars",
                severity=Severity.CRITICAL,
                message=f"{bad_count}/{len(bars)} bars ({bad_pct:.0f}%) failed quality checks",
            ))
            report.passed = False
        elif bad_count > 0:
            report.issues.append(QualityIssue(
                field="bars",
                severity=Severity.WARNING,
                message=f"{bad_count}/{len(bars)} bars dropped, {len(cleaned_bars)} remaining",
            ))
            report.passed = True
        else:
            report.passed = True

        report.bar_data = cleaned_bars  # type: ignore
        self._finalize_report(report)
        return report

    # ── Individual checks ─────────────────────────────────────────

    def _check_required_fields(
        self, bar: Dict[str, Any], issues: List[QualityIssue]
    ) -> None:
        for fld in REQUIRED_FIELDS:
            if fld not in bar:
                issues.append(QualityIssue(
                    field=fld, severity=Severity.CRITICAL,
                    message=f"Required field '{fld}' missing from bar data",
                ))

    def _check_nan_values(
        self, bar: Dict[str, Any], issues: List[QualityIssue]
    ) -> None:
        for fld in REQUIRED_FIELDS:
            val = bar.get(fld)
            if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
                issues.append(QualityIssue(
                    field=fld, severity=Severity.CRITICAL,
                    message=f"'{fld}' is {val!r} (NaN/None/Inf)",
                    value=val,
                ))

        # Check metadata fields (warning, not critical — can use defaults)
        for fld in METADATA_FIELDS:
            val = bar.get(fld)
            if val is not None and isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                defaults = {"implied_vol": 0.20, "risk_free_rate": 0.045, "vix_close": 20.0}
                fixed = defaults.get(fld, 0.0)
                issues.append(QualityIssue(
                    field=fld, severity=Severity.WARNING,
                    message=f"'{fld}' is {val!r}, replaced with default {fixed}",
                    value=val, fixed_value=fixed,
                ))
                bar[fld] = fixed

    def _check_price_sanity(
        self, bar: Dict[str, Any], issues: List[QualityIssue]
    ) -> None:
        lo, hi = self.spy_price_range
        for fld in ["open", "high", "low", "close"]:
            val = bar.get(fld)
            if val is None:
                continue
            if not isinstance(val, (int, float)):
                issues.append(QualityIssue(
                    field=fld, severity=Severity.CRITICAL,
                    message=f"'{fld}' is not numeric: {val!r} (type={type(val).__name__})",
                    value=val,
                ))
                continue
            if val < lo or val > hi:
                issues.append(QualityIssue(
                    field=fld, severity=Severity.CRITICAL,
                    message=f"'{fld}'={val} outside valid range [{lo}, {hi}]",
                    value=val,
                ))

    def _check_ohlc_consistency(
        self, bar: Dict[str, Any], issues: List[QualityIssue]
    ) -> None:
        o, h, l, c = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")
        if not all(isinstance(v, (int, float)) for v in [o, h, l, c] if v is not None):
            return

        if h is not None and l is not None:
            if h < l:
                issues.append(QualityIssue(
                    field="high/low", severity=Severity.WARNING,
                    message=f"high ({h}) < low ({l}), swapping",
                    value=(h, l), fixed_value=(l, h),
                ))
                bar["high"], bar["low"] = l, h

            if o is not None:
                if o > h or o < l:
                    sev = Severity.WARNING if self.strict else Severity.INFO
                    issues.append(QualityIssue(
                        field="open", severity=sev,
                        message=f"open ({o}) outside high-low range [{l}, {h}]",
                        value=o,
                    ))

    def _check_metadata(
        self, bar: Dict[str, Any], issues: List[QualityIssue]
    ) -> None:
        # VIX check
        vix = bar.get("vix_close")
        if vix is not None:
            if vix < self.vix_range[0] or vix > self.vix_range[1]:
                issues.append(QualityIssue(
                    field="vix_close", severity=Severity.WARNING,
                    message=f"VIX={vix} outside range [{self.vix_range[0]}, {self.vix_range[1]}], using 20.0",
                    value=vix, fixed_value=20.0,
                ))
                bar["vix_close"] = 20.0
                bar["implied_vol"] = 0.20

        # Implied vol check
        iv = bar.get("implied_vol")
        if iv is not None:
            if iv < self.iv_range[0] or iv > self.iv_range[1]:
                issues.append(QualityIssue(
                    field="implied_vol", severity=Severity.WARNING,
                    message=f"IV={iv} outside range [{self.iv_range[0]}, {self.iv_range[1]}], using 0.20",
                    value=iv, fixed_value=0.20,
                ))
                bar["implied_vol"] = 0.20

        # Risk-free rate check
        rf = bar.get("risk_free_rate")
        if rf is not None:
            if rf < self.rf_range[0] or rf > self.rf_range[1]:
                issues.append(QualityIssue(
                    field="risk_free_rate", severity=Severity.WARNING,
                    message=f"RF={rf} outside range [{self.rf_range[0]}, {self.rf_range[1]}], using 0.045",
                    value=rf, fixed_value=0.045,
                ))
                bar["risk_free_rate"] = 0.045

    def _check_timestamp(
        self, bar: Dict[str, Any], issues: List[QualityIssue]
    ) -> None:
        ts = bar.get("timestamp")
        if ts is None:
            issues.append(QualityIssue(
                field="timestamp", severity=Severity.WARNING,
                message="No timestamp in bar data",
            ))
            return

        # Parse timestamp if string
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                issues.append(QualityIssue(
                    field="timestamp", severity=Severity.WARNING,
                    message=f"Cannot parse timestamp: {ts!r}",
                    value=ts,
                ))
                return

        # Make naive for comparison
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)

        now = datetime.utcnow()
        age_hours = (now - ts).total_seconds() / 3600

        if age_hours > self.max_staleness_hours:
            issues.append(QualityIssue(
                field="timestamp", severity=Severity.WARNING,
                message=f"Data is {age_hours:.0f}h old (threshold: {self.max_staleness_hours}h)",
                value=ts,
            ))

        # Track for gap detection
        self._last_bar_time = ts

    def _check_extreme_moves(
        self, bar: Dict[str, Any], issues: List[QualityIssue]
    ) -> None:
        close = bar.get("close")
        if close is None or not isinstance(close, (int, float)):
            return

        if self._recent_closes:
            prev = self._recent_closes[-1]
            if prev > 0:
                change_pct = abs(close - prev) / prev * 100
                if change_pct > 15:
                    issues.append(QualityIssue(
                        field="close",
                        severity=Severity.WARNING if change_pct < 25 else Severity.CRITICAL,
                        message=f"Extreme move: {change_pct:.1f}% from previous close ({prev:.2f} → {close:.2f})",
                        value=close,
                    ))

        self._recent_closes.append(close)
        # Keep only last 10
        if len(self._recent_closes) > 10:
            self._recent_closes = self._recent_closes[-10:]

    # ── Helpers ───────────────────────────────────────────────────

    def _finalize_report(self, report: QualityReport) -> None:
        """Log report and raise if critical."""
        if report.has_critical:
            for issue in report.issues:
                if issue.severity == Severity.CRITICAL:
                    logger.error(
                        "data_quality_critical",
                        field=issue.field,
                        message=issue.message,
                        value=str(issue.value),
                    )
            raise DataQualityError(report)

        if report.has_warnings:
            for issue in report.issues:
                if issue.severity == Severity.WARNING:
                    logger.warning(
                        "data_quality_warning",
                        field=issue.field,
                        message=issue.message,
                        value=str(issue.value),
                        fixed=str(issue.fixed_value),
                    )
