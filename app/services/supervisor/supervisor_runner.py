"""
Supervisor runner — orchestrates all 22 evaluators sequentially and persists results.

Creates one SupervisorReport + 22 AgentEvaluation rows per run.
Failed or non-participating evaluators produce verdict=NULL rows (not_supervised).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.agent_evaluation import AgentEvaluation
from app.models.conversation import Conversation
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.supervisor_report import (
    SUPERVISOR_REPORT_STATUS_COMPLETED,
    SUPERVISOR_REPORT_STATUS_FAILED,
    SUPERVISOR_REPORT_STATUS_RUNNING,
    SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED,
    SupervisorReport,
)
from app.models.task import Task
from app.services.supervisor.evaluators.aria_conversation_evaluator import (
    AriaConversationEvaluator,
)
from app.services.supervisor.evaluators.atomic_task_generator_evaluator import (
    AtomicTaskGeneratorEvaluator,
)
from app.services.supervisor.evaluators.catalog_selector_evaluator import (
    CatalogSelectorEvaluator,
)
from app.services.supervisor.evaluators.code_change_agent_evaluator import (
    CodeChangeAgentEvaluator,
)
from app.services.supervisor.evaluators.code_change_agent_validator_evaluator import (
    CodeChangeAgentValidatorEvaluator,
)
from app.services.supervisor.evaluators.command_runner_agent_evaluator import (
    CommandRunnerAgentEvaluator,
)
from app.services.supervisor.evaluators.command_runner_agent_validator_evaluator import (
    CommandRunnerAgentValidatorEvaluator,
)
from app.services.supervisor.evaluators.context_selection_agent_evaluator import (
    ContextSelectionAgentEvaluator,
)
from app.services.supervisor.evaluators.document_writer_agent_evaluator import (
    DocumentWriterAgentEvaluator,
)
from app.services.supervisor.evaluators.document_writer_agent_validator_evaluator import (
    DocumentWriterAgentValidatorEvaluator,
)
from app.services.supervisor.evaluators.environment_manager_agent_evaluator import (
    EnvironmentManagerAgentEvaluator,
)
from app.services.supervisor.evaluators.environment_planner_evaluator import (
    EnvironmentPlannerEvaluator,
)
from app.services.supervisor.evaluators.execution_sequencer_evaluator import (
    ExecutionSequencerEvaluator,
)
from app.services.supervisor.evaluators.orchestrator_evaluator import OrchestratorEvaluator
from app.services.supervisor.evaluators.planner_evaluator import PlannerEvaluator
from app.services.supervisor.evaluators.recovery_assignment_evaluator import (
    RecoveryAssignmentEvaluator,
)
from app.services.supervisor.evaluators.recovery_planner_evaluator import (
    RecoveryPlannerEvaluator,
)
from app.services.supervisor.evaluators.requirements_evaluator_evaluator import (
    RequirementsEvaluatorEvaluator,
)
from app.services.supervisor.evaluators.review_episode_evaluator import ReviewEpisodeEvaluator
from app.services.supervisor.evaluators.stage_evaluator_evaluator import StageEvaluatorEvaluator
from app.services.supervisor.evaluators.test_builder_agent_evaluator import (
    TestBuilderAgentEvaluator,
)
from app.services.supervisor.evaluators.test_builder_agent_validator_evaluator import (
    TestBuilderAgentValidatorEvaluator,
)
from app.services.supervisor.supervisor_synthesizer import synthesize

logger = logging.getLogger(__name__)

_EVALUATORS = [
    PlannerEvaluator(),
    AtomicTaskGeneratorEvaluator(),
    ExecutionSequencerEvaluator(),
    EnvironmentPlannerEvaluator(),
    CatalogSelectorEvaluator(),
    OrchestratorEvaluator(),
    ContextSelectionAgentEvaluator(),
    EnvironmentManagerAgentEvaluator(),
    CodeChangeAgentEvaluator(),
    CodeChangeAgentValidatorEvaluator(),
    CommandRunnerAgentEvaluator(),
    CommandRunnerAgentValidatorEvaluator(),
    TestBuilderAgentEvaluator(),
    TestBuilderAgentValidatorEvaluator(),
    DocumentWriterAgentEvaluator(),
    DocumentWriterAgentValidatorEvaluator(),
    StageEvaluatorEvaluator(),
    RecoveryPlannerEvaluator(),
    RecoveryAssignmentEvaluator(),
    RequirementsEvaluatorEvaluator(),
    ReviewEpisodeEvaluator(),
    AriaConversationEvaluator(),
]

_VERDICT_SCORE = {"healthy": 2, "needs_attention": 1, "degraded": 0}

# Thresholds for the score-based overall verdict.
# Score = average of numeric scores across supervised agents (not_supervised excluded).
# healthy >= 1.7 > needs_attention >= 1.3 > degraded
_HEALTHY_THRESHOLD = 1.7
_NEEDS_ATTENTION_THRESHOLD = 1.3

# If strictly more than this fraction of agents are not_supervised, the sample
# is too incomplete to produce a meaningful verdict.
_NOT_EVALUATED_THRESHOLD = 0.30


def _overall_verdict(verdicts: list[str | None]) -> str:
    if not verdicts:
        return "healthy"

    not_supervised_count = sum(1 for v in verdicts if v is None)
    if not_supervised_count / len(verdicts) > _NOT_EVALUATED_THRESHOLD:
        return SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED

    scores = [_VERDICT_SCORE[v] for v in verdicts if v in _VERDICT_SCORE]
    if not scores:
        return SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED
    avg = sum(scores) / len(scores)
    if avg >= _HEALTHY_THRESHOLD:
        return "healthy"
    if avg >= _NEEDS_ATTENTION_THRESHOLD:
        return "needs_attention"
    return "degraded"


def _get_system_version(db: Session, project_id: int) -> str | None:
    row = (
        db.query(ExecutionRun.system_version)
        .join(Task, Task.id == ExecutionRun.task_id)
        .filter(Task.project_id == project_id, ExecutionRun.system_version.isnot(None))
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    return row[0] if row else None


def run_supervisor(*, db: Session, project_id: int) -> SupervisorReport:
    """Run all evaluators and persist results. Returns the completed SupervisorReport."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    conversation = db.query(Conversation).filter(Conversation.project_id == project_id).first()
    requirements_draft = conversation.requirements_draft if conversation else ""

    system_version = _get_system_version(db, project_id)

    report = SupervisorReport(
        project_id=project_id,
        status=SUPERVISOR_REPORT_STATUS_RUNNING,
    )
    db.add(report)
    db.flush()

    all_run_ids: set[int] = set()
    agent_results_for_synthesis: list[dict] = []
    saved_verdicts: list[str | None] = []

    for evaluator in _EVALUATORS:
        agent_name = evaluator.AGENT_NAME
        try:
            output = evaluator.evaluate(
                db=db,
                project_id=project_id,
                project_name=project.name,
                project_description=project.description or "",
                system_version=system_version,
            )
        except Exception as exc:
            logger.warning(
                "supervisor_evaluator_failed agent=%s project_id=%s error=%s",
                agent_name,
                project_id,
                exc,
                exc_info=True,
            )
            row = AgentEvaluation(
                report_id=report.report_id,
                agent_name=agent_name,
                project_id=project_id,
                verdict=None,
                findings=f"Evaluator failed with error: {exc}",
                issues=[],
                suggestions=[],
                execution_run_ids_analyzed=[],
                system_versions_seen=[],
            )
            db.add(row)
            saved_verdicts.append(None)
            agent_results_for_synthesis.append(
                {
                    "agent_name": agent_name,
                    "verdict": None,
                    "findings": f"Evaluator failed with error: {exc}",
                    "issues": [],
                    "suggestions": [],
                }
            )
            continue

        all_run_ids.update(output.execution_run_ids_analyzed)

        result = output.result
        if result is None:
            row = AgentEvaluation(
                report_id=report.report_id,
                agent_name=agent_name,
                project_id=project_id,
                verdict=None,
                findings=None,
                issues=None,
                suggestions=None,
                execution_run_ids_analyzed=output.execution_run_ids_analyzed or None,
                system_versions_seen=output.system_versions_seen or None,
            )
        else:
            row = AgentEvaluation(
                report_id=report.report_id,
                agent_name=agent_name,
                project_id=project_id,
                verdict=result.verdict,
                findings=result.findings,
                issues=result.issues,
                suggestions=result.suggestions,
                execution_run_ids_analyzed=output.execution_run_ids_analyzed or None,
                system_versions_seen=output.system_versions_seen or None,
            )

        db.add(row)
        saved_verdicts.append(result.verdict if result else None)
        agent_results_for_synthesis.append(
            {
                "agent_name": agent_name,
                "verdict": result.verdict if result else None,
                "findings": result.findings if result else None,
                "issues": result.issues if result else [],
                "suggestions": result.suggestions if result else [],
            }
        )

    verdict = _overall_verdict(saved_verdicts)

    try:
        synthesis_text = synthesize(
            project_id=project_id,
            project_name=project.name,
            project_description=project.description or "",
            requirements_draft=requirements_draft or "",
            overall_verdict=verdict,
            agent_results=agent_results_for_synthesis,
        )
    except Exception as exc:
        logger.error(
            "supervisor_synthesis_failed project_id=%s error=%s", project_id, exc, exc_info=True
        )
        synthesis_text = f"Synthesis generation failed: {exc}"

    report.status = SUPERVISOR_REPORT_STATUS_COMPLETED
    report.overall_verdict = verdict
    report.synthesis = synthesis_text
    report.analyzed_run_ids = sorted(all_run_ids)
    report.completed_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(report)
    except Exception as exc:
        db.rollback()
        report.status = SUPERVISOR_REPORT_STATUS_FAILED
        report.error_message = str(exc)
        db.commit()
        raise

    logger.info(
        "supervisor_completed project_id=%s report_id=%s verdict=%s",
        project_id,
        report.report_id,
        verdict,
    )
    return report
