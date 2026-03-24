from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution_engine.base import (
    BaseExecutionEngine,
    ExecutionEngineError,
    ExecutionEngineRejectedError,
)
from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    CHANGE_TYPE_MODIFIED,
    EXECUTION_DECISION_FAILED,
    EXECUTION_DECISION_PARTIAL,
    EXECUTION_DECISION_REJECTED,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    ChangedFile,
)
from app.models.task import Task
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
    CodeExecutorResult,
)
from app.services.code_executor import (
    CodeExecutorInternalError,
    CodeExecutorRejectedError,
    LocalCodeExecutor,
)


class LegacyLocalExecutionEngine(BaseExecutionEngine):
    backend_name = "legacy_local"

    def __init__(self, db: Session, budget) -> None:
        super().__init__(budget=budget)
        self.db = db

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        task = self.db.get(Task, request.task_id)
        if not task:
            raise ExecutionEngineError(f"Task {request.task_id} not found")

        try:
            legacy_executor = LocalCodeExecutor(db=self.db)
            legacy_result = legacy_executor.execute(
                task=task,
                execution_run_id=request.execution_run_id,
            )
            return self._from_legacy_result(legacy_result)

        except CodeExecutorRejectedError as exc:
            raise ExecutionEngineRejectedError(
                message=exc.message,
                rejection_reason=exc.message,
                remaining_scope=exc.remaining_scope,
                blockers_found=[exc.blockers_found] if exc.blockers_found else [],
                validation_notes=[
                    "Execution was rejected at the legacy_local engine boundary.",
                ],
                failure_code=exc.failure_code,
            ) from exc

        except CodeExecutorInternalError as exc:
            return ExecutionResult(
                task_id=request.task_id,
                decision=EXECUTION_DECISION_FAILED,
                summary=exc.message,
                details="The legacy local execution engine failed internally.",
                output_snapshot="execution_engine_failed",
                blockers_found=[],
                validation_notes=[f"failure_code={exc.failure_code}"],
                evidence=ExecutionEvidence(),
            )

        except Exception as exc:
            return ExecutionResult(
                task_id=request.task_id,
                decision=EXECUTION_DECISION_FAILED,
                summary=f"Unexpected execution engine failure: {str(exc)}",
                details="Unexpected failure outside the legacy code executor flow.",
                output_snapshot="execution_engine_failed",
                blockers_found=[],
                validation_notes=["Unexpected execution engine failure."],
                evidence=ExecutionEvidence(),
            )

    def _from_legacy_result(self, result: CodeExecutorResult) -> ExecutionResult:
        changed_files: list[ChangedFile] = []

        for path in result.workspace_changes.created_files:
            changed_files.append(
                ChangedFile(path=path, change_type=CHANGE_TYPE_CREATED)
            )

        for path in result.workspace_changes.modified_files:
            changed_files.append(
                ChangedFile(path=path, change_type=CHANGE_TYPE_MODIFIED)
            )

        artifact_refs = [
            note.strip()
            for note in result.journal.notes_for_validator
            if "artifact_id=" in note
        ]

        return ExecutionResult(
            task_id=result.task_id,
            decision=self._map_legacy_status(result.execution_status),
            summary=result.journal.summary,
            details=result.edit_plan.summary,
            rejection_reason=None,
            completed_scope=result.journal.claimed_completed_scope,
            remaining_scope=result.journal.claimed_remaining_scope,
            blockers_found=result.journal.encountered_uncertainties,
            validation_notes=result.journal.notes_for_validator,
            output_snapshot=result.output_snapshot,
            evidence=ExecutionEvidence(
                changed_files=changed_files,
                commands=[],
                notes=result.edit_plan.notes,
                artifacts_created=artifact_refs,
            ),
        )

    @staticmethod
    def _map_legacy_status(status: str) -> str:
        if status == CODE_EXECUTION_STATUS_AWAITING_VALIDATION:
            # El engine no es el dueño del cierre final de la task.
            # Si terminó su trabajo y debe pasar por validator, lo tratamos como partial.
            return EXECUTION_DECISION_PARTIAL
        if status == CODE_EXECUTION_STATUS_FAILED:
            return EXECUTION_DECISION_FAILED
        if status == CODE_EXECUTION_STATUS_REJECTED:
            return EXECUTION_DECISION_REJECTED
        return EXECUTION_DECISION_FAILED