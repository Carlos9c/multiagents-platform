"""WebSocket ConnectionManager.

Manages active WebSocket connections per project and provides both async
and sync-safe broadcast methods. The sync path (broadcast_sync) is used
by the execution layer to push review notifications from synchronous code.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Module-level event loop reference — set from the FastAPI startup event.
_loop: asyncio.AbstractEventLoop | None = None


class ConnectionManager:
    def __init__(self) -> None:
        # project_id → set of active WebSocket connections
        self._connections: dict[int, set[WebSocket]] = {}

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        global _loop
        _loop = loop

    async def connect(self, websocket: WebSocket, project_id: int) -> None:
        await websocket.accept()
        self._connections.setdefault(project_id, set()).add(websocket)
        logger.info(
            "ws_connected project_id=%s total=%s", project_id, len(self._connections[project_id])
        )

    def disconnect(self, websocket: WebSocket, project_id: int) -> None:
        bucket = self._connections.get(project_id)
        if bucket:
            bucket.discard(websocket)
        logger.info("ws_disconnected project_id=%s", project_id)

    async def broadcast(self, project_id: int, message: dict[str, Any]) -> None:
        """Send a JSON message to all clients connected to a project."""
        bucket = self._connections.get(project_id, set()).copy()
        dead: set[WebSocket] = set()
        for ws in bucket:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._connections.get(project_id, set()).discard(ws)

    def broadcast_sync(self, project_id: int, message: dict[str, Any]) -> None:
        """Thread-safe broadcast for calling from synchronous (non-async) code."""
        if _loop is None or _loop.is_closed():
            logger.warning("broadcast_sync called but no event loop is registered")
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(project_id, message), _loop)

    def has_connections(self, project_id: int) -> bool:
        return bool(self._connections.get(project_id))


# Singleton used across the app
manager = ConnectionManager()
