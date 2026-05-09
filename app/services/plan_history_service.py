from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PLAN_HISTORY_FILENAME = "plan_history.json"


class PlanHistoryService:
    """
    Manages the plan history log stored in project_meta/plan_history.json.

    Each entry records one planning cycle: when it happened, what the objective
    was, and which source path was used (if any). This file is the historical
    record of all evolutionary cycles run on a project.
    """

    def record_cycle(
        self,
        project_meta_dir: Path,
        objective: str,
        source_path: str | None = None,
    ) -> None:
        history = self._read(project_meta_dir)
        history.append(
            {
                "planned_at": datetime.now(timezone.utc).isoformat(),
                "objective": objective,
                "source_path": source_path,
            }
        )
        self._write(project_meta_dir, history)
        logger.info(
            "plan_history_cycle_recorded project_meta_dir=%s cycles=%d",
            str(project_meta_dir),
            len(history),
        )

    def get_history(self, project_meta_dir: Path) -> list[dict]:
        return self._read(project_meta_dir)

    def _read(self, project_meta_dir: Path) -> list[dict]:
        path = project_meta_dir / PLAN_HISTORY_FILENAME
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("plan_history_read_error path=%s", str(path))
            return []

    def _write(self, project_meta_dir: Path, history: list[dict]) -> None:
        path = project_meta_dir / PLAN_HISTORY_FILENAME
        path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
