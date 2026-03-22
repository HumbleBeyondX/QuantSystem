"""Serialization utilities for Kafka messages and storage."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict
from uuid import UUID


class QuantJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime, UUID, and enum types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, UUID):
            return str(obj)
        if hasattr(obj, "value"):  # Enum
            return obj.value
        if hasattr(obj, "__dataclass_fields__"):  # Dataclass
            return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
        return super().default(obj)


def to_json(data: Any) -> str:
    """Serialize data to JSON string."""
    return json.dumps(data, cls=QuantJSONEncoder)


def to_json_bytes(data: Any) -> bytes:
    """Serialize data to JSON bytes (for Kafka)."""
    return to_json(data).encode("utf-8")


def from_json(data: str | bytes) -> Any:
    """Deserialize JSON string/bytes to Python object."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)


def event_to_dict(event: Any) -> Dict[str, Any]:
    """Convert an Event dataclass to a serializable dict."""
    if hasattr(event, "__dataclass_fields__"):
        result = {}
        for field_name in event.__dataclass_fields__:
            value = getattr(event, field_name)
            if isinstance(value, datetime):
                result[field_name] = value.isoformat()
            elif isinstance(value, UUID):
                result[field_name] = str(value)
            elif hasattr(value, "value"):  # Enum
                result[field_name] = value.value
            elif isinstance(value, dict):
                result[field_name] = value
            else:
                result[field_name] = value
        return result
    return {"data": str(event)}
