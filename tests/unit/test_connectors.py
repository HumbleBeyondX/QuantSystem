"""Unit tests for data connectors, normaliser, and validator."""

from __future__ import annotations

from datetime import datetime, timedelta

from data.connectors.polygon_connector import PolygonConnector
from data.pipeline.normalizer import DataNormalizer
from data.pipeline.validator import DataValidator


# ---------------------------------------------------------------------------
# Polygon Connector Normalisation
# ---------------------------------------------------------------------------


class TestPolygonNormalize:
    def _connector(self) -> PolygonConnector:
        return PolygonConnector(api_key="test_key")

    def test_polygon_normalize_trade(self):
        """Polygon trade event (ev=T) should normalise to a trade dict."""
        conn = self._connector()
        raw = {
            "ev": "T",
            "sym": "AAPL",
            "p": 192.50,
            "s": 100,
            "t": 1717243800000,
        }
        result = conn._normalize_event(raw)

        assert result is not None
        assert result["data_type"] == "trade"
        assert result["symbol"] == "AAPL"
        assert result["price"] == 192.50
        assert result["amount"] == 100

    def test_polygon_normalize_quote(self):
        """Polygon quote event (ev=Q) should normalise to a quote dict."""
        conn = self._connector()
        raw = {
            "ev": "Q",
            "sym": "AAPL",
            "bp": 192.40,
            "ap": 192.60,
            "bs": 300,
            "as": 200,
            "t": 1717243800000,
        }
        result = conn._normalize_event(raw)

        assert result is not None
        assert result["data_type"] == "quote"
        assert result["bid"] == 192.40
        assert result["ask"] == 192.60

    def test_polygon_normalize_bar(self):
        """Polygon aggregate minute event (ev=AM) should normalise to a bar dict."""
        conn = self._connector()
        raw = {
            "ev": "AM",
            "sym": "AAPL",
            "o": 192.00,
            "h": 193.00,
            "l": 191.50,
            "c": 192.80,
            "v": 50_000,
            "vw": 192.40,
            "s": 1717243800000,
        }
        result = conn._normalize_event(raw)

        assert result is not None
        assert result["data_type"] == "bar"
        assert result["open"] == 192.00
        assert result["close"] == 192.80
        assert result["vwap"] == 192.40

    def test_polygon_unknown_event_returns_none(self):
        """An unknown event type should return None."""
        conn = self._connector()
        result = conn._normalize_event({"ev": "status", "message": "ok"})
        assert result is None


# ---------------------------------------------------------------------------
# Data Validator
# ---------------------------------------------------------------------------


class TestDataValidator:
    def test_data_validator_crossed_market(self):
        """A crossed market (bid > ask) should be flagged as invalid."""
        validator = DataValidator()
        result = validator.validate_tick(
            symbol="AAPL",
            bid=193.0,
            ask=192.0,
            last=192.5,
            timestamp=datetime.utcnow(),
        )

        assert result.is_valid is False
        assert any("Crossed market" in e for e in result.errors)

    def test_data_validator_stale_data(self):
        """Data older than the stale threshold should produce a warning."""
        validator = DataValidator(stale_threshold_sec=30.0)
        old_ts = datetime.utcnow() - timedelta(seconds=60)

        result = validator.validate_tick(
            symbol="AAPL",
            bid=192.0,
            ask=192.5,
            last=192.3,
            timestamp=old_ts,
        )

        assert result.is_valid is True  # stale data is a warning, not an error
        assert any("Stale data" in w for w in result.warnings)

    def test_data_validator_valid_tick(self):
        """A normal tick should pass all validations."""
        validator = DataValidator()
        result = validator.validate_tick(
            symbol="AAPL",
            bid=192.0,
            ask=192.5,
            last=192.3,
            timestamp=datetime.utcnow(),
        )
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_data_validator_bar_high_less_than_low(self):
        """A bar with high < low should be flagged as invalid."""
        validator = DataValidator()
        result = validator.validate_bar(
            symbol="AAPL",
            open_=192.0,
            high=190.0,  # invalid: high < low
            low=193.0,
            close=191.5,
            volume=1000.0,
        )
        assert result.is_valid is False
        assert any("High" in e and "Low" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Data Normaliser
# ---------------------------------------------------------------------------


class TestNormalizer:
    def test_normalizer_tick(self):
        """DataNormalizer.normalize_tick should produce a Tick from raw dict."""
        raw = {
            "symbol": "AAPL",
            "bid": 192.0,
            "ask": 192.5,
            "last": 192.3,
            "volume": 500,
            "timestamp": datetime.utcnow(),
            "source": "polygon",
        }
        tick = DataNormalizer.normalize_tick(raw)
        assert tick is not None
        assert tick.symbol == "AAPL"
        assert tick.bid == 192.0

    def test_normalizer_trade(self):
        """DataNormalizer.normalize_trade should produce a Trade from raw dict."""
        raw = {
            "symbol": "AAPL",
            "price": 192.5,
            "amount": 100,
            "side": "BUY",
            "timestamp": datetime.utcnow(),
            "source": "polygon",
        }
        trade = DataNormalizer.normalize_trade(raw)
        assert trade is not None
        assert trade.price == 192.5

    def test_normalizer_bar(self):
        """DataNormalizer.normalize_bar should produce a Bar from raw dict."""
        raw = {
            "symbol": "AAPL",
            "open": 192.0,
            "high": 193.0,
            "low": 191.5,
            "close": 192.8,
            "volume": 50_000,
            "timeframe": "1m",
            "timestamp": datetime.utcnow(),
            "source": "polygon",
        }
        bar = DataNormalizer.normalize_bar(raw)
        assert bar is not None
        assert bar.open == 192.0
        assert bar.close == 192.8

    def test_normalizer_missing_field_returns_none(self):
        """Normalisation should return None when required fields are missing."""
        raw = {"bid": 100.0}  # missing 'symbol'
        assert DataNormalizer.normalize_tick(raw) is None
