from __future__ import annotations

import threading

from app.services.environment.contracts import EnvironmentSession


class EnvironmentSessionStore:
    """Thread-safe in-memory store for active environment sessions.

    Keyed by project_id. Only one active session per project at a time.
    Sessions are lost on process restart; containers must be cleaned up separately.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, EnvironmentSession] = {}
        self._lock = threading.Lock()

    def set_session(self, project_id: int, session: EnvironmentSession) -> None:
        with self._lock:
            self._sessions[project_id] = session

    def get_session(self, project_id: int) -> EnvironmentSession | None:
        with self._lock:
            return self._sessions.get(project_id)

    def remove_session(self, project_id: int) -> None:
        with self._lock:
            self._sessions.pop(project_id, None)

    def get_all_sessions(self) -> dict[int, EnvironmentSession]:
        with self._lock:
            return dict(self._sessions)


_default_store = EnvironmentSessionStore()


def get_session_store() -> EnvironmentSessionStore:
    return _default_store
