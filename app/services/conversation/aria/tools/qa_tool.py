"""QATool — handles all QA-related interactions inside the Aria loop.

Dispatches to one of four situations based on conversation state:

  A — qa_offer_pending=True
        · hint=None  → initial offer (triggered by PROJECT_COMPLETED event)
        · hint set   → user responded to offer; evaluate yes/no

  B — phase=qa_running, user sends a message
        → static "QA in progress" response (no LLM needed)

  C — pending_qa_report is set, hint=None
        → QA just completed; summarise findings for Aria

  D — pending_qa_report is set, hint is set
        → user responded to the remediation offer; evaluate yes/no
          and create Task objects if confirmed

Phase transitions are applied by the orchestrator after execute() returns,
based on the `status` key in the returned ToolResult.data dict.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_QA_RUNNING,
    Conversation,
)
from app.models.task import (
    EXECUTION_ENGINE,
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_PENDING,
    Task,
)
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

# Dedicated thread pool for background QA runs launched from within Aria.
_qa_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="aria_qa")

# ── LLM output schema ─────────────────────────────────────────────────────────

_SYSTEM_PROMPT = prompt_loader.get("qa_tool", "evaluate_user_response")


class _EvalOutput(BaseModel):
    accepted: bool
    reasoning: str


# ── QATool ────────────────────────────────────────────────────────────────────


class QATool:
    """Manages the full QA offer / run / findings / remediation lifecycle."""

    @property
    def name(self) -> ToolName:
        return ToolName.QA

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str | None = None,
    ) -> ToolResult:
        # ── Situation B ───────────────────────────────────────────────────────
        if conversation.phase == CONVERSATION_PHASE_QA_RUNNING:
            return ToolResult(
                tool_name=ToolName.QA,
                data={
                    "status": "qa_running",
                    "message": "El análisis QA está en curso, en breve tendrás los resultados.",
                },
            )

        # ── Situation C / D: there is a stored QA report ──────────────────────
        if conversation.pending_qa_report:
            if hint:
                # D — user is responding to the remediation question
                return self._handle_remediation_response(db, project_id, conversation, hint)
            # C — first call after QA_COMPLETED; summarise for Aria
            return self._summarize_qa_result(conversation)

        # ── Situation A: offer is pending ─────────────────────────────────────
        if conversation.qa_offer_pending:
            if hint:
                # A2 — user responded to the offer
                return self._handle_offer_response(db, project_id, conversation, hint)
            # A1 — initial call, build the offer payload for Aria
            return self._make_qa_offer(db, project_id, conversation)

        # Fallback — no QA state is active
        return ToolResult(
            tool_name=ToolName.QA,
            data={"status": "no_action"},
        )

    # ── Situation A helpers ───────────────────────────────────────────────────

    def _make_qa_offer(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
    ) -> ToolResult:
        """Build the offer payload (no LLM needed — Aria composes the message)."""
        from app.models.project import Project
        from app.models.task import (
            TASK_STATUS_COMPLETED,
            TASK_STATUS_FAILED,
            Task,
        )

        project = db.get(Project, project_id)
        project_name = project.name if project else f"Proyecto {project_id}"

        completed_count = (
            db.query(Task)
            .filter(
                Task.project_id == project_id,
                Task.planning_level == PLANNING_LEVEL_ATOMIC,
                Task.status == TASK_STATUS_COMPLETED,
            )
            .count()
        )
        failed_count = (
            db.query(Task)
            .filter(
                Task.project_id == project_id,
                Task.planning_level == PLANNING_LEVEL_ATOMIC,
                Task.status == TASK_STATUS_FAILED,
            )
            .count()
        )

        return ToolResult(
            tool_name=ToolName.QA,
            data={
                "status": "qa_offer",
                "project_name": project_name,
                "completed_tasks": completed_count,
                "failed_tasks": failed_count,
            },
        )

    def _handle_offer_response(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str,
    ) -> ToolResult:
        """Evaluate yes/no; start QA run or decline."""
        accepted = _evaluate_yes_no(
            context="Se ofreció ejecutar el QA engine para analizar el proyecto.",
            user_response=hint,
        )

        if not accepted:
            return ToolResult(
                tool_name=ToolName.QA,
                data={"status": "qa_offer_declined"},
            )

        # Launch QA run
        from app.services.qa.qa_runner import create_qa_session, run_qa_background

        qa_session = create_qa_session(db, project_id)
        db.commit()

        _qa_executor.submit(run_qa_background, project_id, qa_session.id)

        return ToolResult(
            tool_name=ToolName.QA,
            data={
                "status": "qa_started",
                "qa_session_id": qa_session.id,
            },
            side_effects_summary=f"QA sesión {qa_session.id} iniciada en background.",
        )

    # ── Situation C helper ────────────────────────────────────────────────────

    def _summarize_qa_result(self, conversation: Conversation) -> ToolResult:
        """Parse the stored QA report and return a summary for Aria."""
        try:
            data = json.loads(conversation.pending_qa_report)  # type: ignore[arg-type]
        except (TypeError, json.JSONDecodeError):
            return ToolResult(
                tool_name=ToolName.QA,
                data={
                    "status": "qa_completed_no_issues",
                    "verdict": "unknown",
                    "error": "Could not parse QA report",
                },
            )

        verdict = data.get("verdict", "unknown")
        findings = data.get("findings", [])
        findings_summary = data.get("findings_summary") or {}
        remediation_tasks = data.get("remediation_tasks", [])
        error_message = data.get("error_message")

        has_findings = bool(findings) or bool(
            findings_summary
            and any(v for v in findings_summary.values() if isinstance(v, int) and v > 0)
        )
        has_remediation = bool(remediation_tasks)

        if has_findings or has_remediation:
            status = "qa_completed_with_findings"
        else:
            status = "qa_completed_no_issues"

        return ToolResult(
            tool_name=ToolName.QA,
            data={
                "status": status,
                "verdict": verdict,
                "has_findings": has_findings,
                "findings_summary": findings_summary,
                "total_findings": (
                    len(findings)
                    if findings
                    else sum(v for v in findings_summary.values() if isinstance(v, int))
                ),
                "remediation_tasks": remediation_tasks,
                "has_remediation_tasks": has_remediation,
                "error_message": error_message,
            },
        )

    # ── Situation D helper ────────────────────────────────────────────────────

    def _handle_remediation_response(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str,
    ) -> ToolResult:
        """Evaluate yes/no; create remediation Tasks or decline."""
        accepted = _evaluate_yes_no(
            context="Se ofreció crear tareas de remediación en el backlog del proyecto.",
            user_response=hint,
        )

        if not accepted:
            return ToolResult(
                tool_name=ToolName.QA,
                data={"status": "remediation_declined"},
            )

        # Parse remediation tasks from the stored report
        tasks_created = 0
        try:
            report = json.loads(conversation.pending_qa_report)  # type: ignore[arg-type]
            remediation_tasks = report.get("remediation_tasks", [])
            for rt in remediation_tasks:
                task = Task(
                    project_id=project_id,
                    title=rt.get("title", "Remediation task"),
                    description=rt.get("description", ""),
                    priority=rt.get("priority", "medium"),
                    task_type="implementation",
                    planning_level=PLANNING_LEVEL_ATOMIC,
                    executor_type=EXECUTION_ENGINE,
                    status=TASK_STATUS_PENDING,
                )
                db.add(task)
                tasks_created += 1
            db.flush()
        except Exception:
            logger.warning(
                "qa_tool_remediation_task_creation_failed project_id=%s",
                project_id,
                exc_info=True,
            )

        return ToolResult(
            tool_name=ToolName.QA,
            data={
                "status": "remediation_confirmed",
                "tasks_created": tasks_created,
            },
            side_effects_summary=(f"{tasks_created} tarea(s) de remediación añadidas al backlog."),
        )


# ── LLM helper ────────────────────────────────────────────────────────────────


def _evaluate_yes_no(context: str, user_response: str) -> bool:
    """Return True if the user's response signals acceptance."""
    from app.services.llm.schema_utils import to_openai_strict_json_schema

    try:
        provider = get_llm_provider()
        schema = to_openai_strict_json_schema(_EvalOutput.model_json_schema())
        raw = provider.generate_structured(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=f"CONTEXT:\n{context}\n\nUSER RESPONSE:\n{user_response}",
            schema_name="qa_eval_output",
            json_schema=schema,
        )
        result = _EvalOutput.model_validate(raw)
        return result.accepted
    except Exception:
        logger.warning("qa_tool_eval_yes_no_failed — defaulting to declined", exc_info=True)
        return False
