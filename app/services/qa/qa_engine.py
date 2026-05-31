"""QAEngine — top-level entry point for a single QA run.

Orchestrates:
  1. Strategy selection (by product_type)
  2. Bootstrapping (artifact compilation if needed)
  3. QA orchestration (probing → synthesis)
  4. DB persistence (QASession status / verdict update)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.qa_session import (
    QA_SESSION_STATUS_BLOCKED,
    QA_SESSION_STATUS_COMPLETED,
)
from app.models.qa_session import (
    QASession as QASessionModel,
)
from app.services.qa.agents.registry import get_agent_registry
from app.services.qa.budget import QABudget
from app.services.qa.contracts import QA_VERDICT_BLOCKED, QARequest, QAResult
from app.services.qa.qa_bootstrapper import QABootstrapper
from app.services.qa.qa_orchestrator import QAOrchestrator
from app.services.qa.strategy_selector import select

logger = logging.getLogger(__name__)


class QAEngineError(Exception):
    """Fatal error that prevents a QA run from starting."""


class QAEngine:
    """Runs a complete QA session for one project.

    Usage:
        engine = QAEngine()
        result = engine.run(db=db, request=request)
    """

    def __init__(
        self,
        *,
        bootstrapper: QABootstrapper | None = None,
        budget: QABudget | None = None,
    ) -> None:
        self._bootstrapper = bootstrapper or QABootstrapper()
        self._budget = budget or QABudget()

    def run(
        self,
        *,
        db: Session,
        request: QARequest,
    ) -> QAResult:
        """Execute the full QA run for the given request.

        Updates the QASession DB record with the final verdict and findings summary.
        Never raises — any internal error is captured as a blocked/failed verdict.
        """
        start = time.monotonic()
        db_session = db.get(QASessionModel, request.qa_session_id)

        try:
            result = self._run_internal(db=db, request=request)
        except Exception as exc:
            logger.exception("qa_engine_fatal_error project_id=%s", request.project_id)
            result = QAResult(
                verdict=QA_VERDICT_BLOCKED,
                error_message=str(exc),
                duration_seconds=round(time.monotonic() - start, 2),
            )

        self._persist_result(db, db_session, result, request=request)
        return result

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run_internal(self, *, db: Session, request: QARequest) -> QAResult:
        # 1. Resolve strategy
        strategy = select(request.product_type)

        # 2. Record which strategy was selected (before bootstrapping may fail)
        db_session = db.get(QASessionModel, request.qa_session_id)
        if db_session is not None and not db_session.strategy_used:
            db_session.strategy_used = strategy.product_type
            db.flush()

        # 3. Bootstrap (compile artifact if required)
        request, blocked = self._bootstrapper.bootstrap(db=db, request=request, strategy=strategy)
        if blocked is not None:
            return blocked

        # 4. Orchestrate
        registry = get_agent_registry()
        orchestrator = QAOrchestrator(registry=registry, budget=self._budget)
        return orchestrator.run(db=db, request=request, strategy=strategy)

    def _persist_result(
        self,
        db: Session,
        db_session: QASessionModel | None,
        result: QAResult,
        *,
        request: QARequest | None = None,
    ) -> None:
        if db_session is None:
            logger.warning(
                "qa_engine_persist_result_skipped — db_session not found " "qa_session_id=%s",
                request.qa_session_id if request else "unknown",
            )
            return
        try:
            # Status reflects the actual outcome: BLOCKED stays blocked,
            # everything else (passed / failed / partial) is COMPLETED.
            if result.verdict == QA_VERDICT_BLOCKED:
                db_session.status = QA_SESSION_STATUS_BLOCKED
            else:
                db_session.status = QA_SESSION_STATUS_COMPLETED

            db_session.verdict = result.verdict
            db_session.agents_called = result.agents_called
            db_session.duration_seconds = result.duration_seconds
            db_session.error_message = result.error_message
            db_session.completed_at = datetime.now(timezone.utc)
            # Store as dict directly — the column is JSON, no manual json.dumps needed.
            db_session.findings_summary = result.findings_summary()
            db_session.probes = [p.model_dump() for p in result.probes]
            if request and not db_session.strategy_used:
                db_session.strategy_used = request.product_type
            db.add(db_session)
            db.flush()
        except Exception:
            logger.warning(
                "qa_engine_persist_result_failed qa_session_id=%s",
                db_session.id,
                exc_info=True,
            )
