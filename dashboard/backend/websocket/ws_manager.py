"""WebSocket connection manager for real-time data push to frontend."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import WebSocket

from core.logging import get_logger

logger = get_logger(__name__)


class WebSocketManager:
    """Manages WebSocket connections and broadcasts data to clients."""

    def __init__(self) -> None:
        self._connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("ws_client_connected", total=len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.remove(websocket)
        logger.info("ws_client_disconnected", total=len(self._connections))

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Send message to all connected clients."""
        disconnected = []
        for ws in self._connections:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self._connections.remove(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


ws_manager = WebSocketManager()
