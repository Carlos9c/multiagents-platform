from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from app.models.task import CODE_EXECUTOR, PLANNING_LEVEL_ATOMIC, Task
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_FILE_ROLE_REFERENCE,
    CODE_FILE_ROLE_RELATED,
    CODE_FILE_ROLE_TARGET,
    CODE_FILE_ROLE_TEST,
    CodeExecutorInput,
    CodeExecutorResult,
    CodeFileContext,
    CodeFileEditPlan,
    CodeWorkingSet,
    ExecutionJournal,
    PlannedFileChange,
    WorkspaceChangeSet,
)
from app.schemas.code_generation import (
    CODE_GENERATION_ACTION_CREATE,
    CODE_GENERATION_ACTION_MODIFY,
    CODE_GENERATION_DECISION_PROCEED,
    CODE_GENERATION_DECISION_REJECT,
)
from app.services.artifacts import create_artifact
from app.services.code_context_selector import (
    CodeContextSelector,
    CodeContextSelectorInternalError,
)
from app.services.code_executor_client import (
    CodeExecutorClientError,
    generate_file_contents,
    plan_file_edits,
)
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService
from app.services.workspace_runtime import PreparedWorkspace, WorkspaceRuntimeError


CODE_EXECUTOR_CONTEXT_ARTIFACT_TYPE = "code_executor_context"
CODE_EXECUTOR_WORKING_SET_ARTIFACT_TYPE = "code_executor_working_set"
CODE_EXECUTOR_EDIT_PLAN_ARTIFACT_TYPE = "code_executor_edit_plan"
CODE_EXECUTOR_RESULT_ARTIFACT_TYPE = "code_executor_result"


class CodeExecutorError(Exception):
    """Base error for code executor domain logic."""


class CodeExecutorRejectedError(CodeExecutorError):
    def __init__(
        self,
        message: str,
        failure_code: str,
        remaining_scope: str | None = None,
        blockers_found: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.failure_code = failure_code
        self.remaining_scope = remaining_scope
        self.blockers_found = blockers_found


class CodeExecutorInternalError(CodeExecutorError):
    def __init__(self, message: str, failure_code: str = "code_executor_internal_error"):
        super().__init__(message)
        self.message = message
        self.failure_code = failure_code


class BaseCodeExecutor(ABC):
    @abstractmethod
    def prepare_workspace(self, task: Task, execution_run_id: int) -> PreparedWorkspace:
        raise NotImplementedError

    @abstractmethod
    def resolve_context(
        self,
        task: Task,
        prepared_workspace: PreparedWorkspace,
    ) -> CodeExecutorInput:
        raise NotImplementedError

    @abstractmethod
    def build_working_set(
        self,
        context: CodeExecutorInput,
        prepared_workspace: PreparedWorkspace,
    ) -> CodeWorkingSet:
        raise NotImplementedError

    @abstractmethod
    def build_edit_plan(
        self,
        task: Task,
        context: CodeExecutorInput,
        working_set: CodeWorkingSet,
    ) -> CodeFileEditPlan:
        raise NotImplementedError

    @abstractmethod
    def apply_changes(
        self,
        task: Task,
        prepared_workspace: PreparedWorkspace,
        context: CodeExecutorInput,
        working_set: CodeWorkingSet,
        plan: CodeFileEditPlan,
    ) -> WorkspaceChangeSet:
        raise NotImplementedError

    @abstractmethod
    def build_journal(
        self,
        context: CodeExecutorInput,
        prepared_workspace: PreparedWorkspace,
        working_set: CodeWorkingSet,
        plan: CodeFileEditPlan,
        changes: WorkspaceChangeSet,
        execution_status: str,
    ) -> ExecutionJournal:
        raise NotImplementedError

    @abstractmethod
    def execute(self, task: Task, execution_run_id: int) -> CodeExecutorResult:
        raise NotImplementedError


class LocalCodeExecutor(BaseCodeExecutor):
    """
    Local filesystem implementation of the code executor.

    Important:
    - code context selection is a targeting aid, not a gatekeeper
    - sparse history must not block execution
    - build_working_set materializes selector roles; it does not invent its own ranking
    """

    def __init__(
        self,
        db: Session,
        storage_service: ProjectStorageService | None = None,
        workspace_runtime: LocalWorkspaceRuntime | None = None,
    ):
        self.db = db
        self.storage_service = storage_service or ProjectStorageService()
        self.workspace_runtime = workspace_runtime or LocalWorkspaceRuntime(
            storage_service=self.storage_service
        )
        self.context_selector = CodeContextSelector(
            db=self.db,
            workspace_runtime=self.workspace_runtime,
        )

    def _reject_if_not_executable(self, task: Task) -> None:
        if task.planning_level != PLANNING_LEVEL_ATOMIC:
            raise CodeExecutorRejectedError(
                message="Task is not atomic and cannot be executed by the code executor.",
                failure_code="non_atomic_task",
                remaining_scope="Atomic task generation must complete before execution.",
                blockers_found="planning_level must be 'atomic'.",
            )

        if task.executor_type != CODE_EXECUTOR:
            raise CodeExecutorRejectedError(
                message=(
                    f"Task executor_type '{task.executor_type}' is not supported by the "
                    f"code executor."
                ),
                failure_code="unsupported_executor_type",
                remaining_scope="Assign a compatible code executor.",
                blockers_found="Incompatible executor assignment.",
            )

        has_minimum_context = any(
            [
                bool(task.description and task.description.strip()),
                bool(task.objective and task.objective.strip()),
                bool(task.implementation_steps and task.implementation_steps.strip()),
            ]
        )

        if not has_minimum_context:
            raise CodeExecutorRejectedError(
                message=(
                    "Task does not have enough context for safe execution. "
                    "At least description, objective, or implementation_steps must exist."
                ),
                failure_code="missing_execution_context",
                remaining_scope="Task must be enriched with executable context.",
                blockers_found="Insufficient execution context.",
            )

    def _persist_phase_artifact(
        self,
        *,
        task: Task,
        artifact_type: str,
        payload_json: str,
    ) -> int:
        artifact = create_artifact(
            db=self.db,
            project_id=task.project_id,
            task_id=task.id,
            artifact_type=artifact_type,
            content=payload_json,
            created_by="code_executor",
        )
        return artifact.id

    @staticmethod
    def _task_stub_from_context(context: CodeExecutorInput) -> Task:
        return Task(
            id=context.task_id,
            project_id=context.project_id,
            title=context.title,
            description=context.description,
            objective=context.objective,
        )

    def prepare_workspace(self, task: Task, execution_run_id: int) -> PreparedWorkspace:
        try:
            self.storage_service.ensure_project_storage(task.project_id)
            self.storage_service.ensure_domain_storage(task.project_id, CODE_DOMAIN)

            return self.workspace_runtime.prepare_workspace(
                project_id=task.project_id,
                execution_run_id=execution_run_id,
                domain_name=CODE_DOMAIN,
            )
        except WorkspaceRuntimeError as exc:
            raise CodeExecutorInternalError(
                message=f"Failed to prepare execution workspace: {str(exc)}",
                failure_code="workspace_prepare_failed",
            ) from exc

    def resolve_context(
        self,
        task: Task,
        prepared_workspace: PreparedWorkspace,
    ) -> CodeExecutorInput:
        self._reject_if_not_executable(task)

        execution_goal = (
            task.objective
            or task.summary
            or task.description
            or f"Execute task {task.id}: {task.title}"
        )

        try:
            selection = self.context_selector.select_context(
                task=task,
                prepared_workspace=prepared_workspace,
                execution_run_id=None,
            )
        except CodeContextSelectorInternalError as exc:
            raise CodeExecutorInternalError(
                message=f"Failed to resolve code context: {str(exc)}",
                failure_code="code_context_selection_failed",
            ) from exc

        primary_targets = [item.path for item in selection.primary_targets]
        related_files = [item.path for item in selection.related_files]
        reference_files = [item.path for item in selection.reference_files]
        related_test_files = [item.path for item in selection.related_test_files]

        candidate_files: list[str] = []
        for path in selection.candidate_file_pool:
            if path not in candidate_files:
                candidate_files.append(path)
        for path in primary_targets + related_files + reference_files + related_test_files:
            if path not in candidate_files:
                candidate_files.append(path)

        relevant_decisions = list(selection.relevant_decisions)
        relevant_decisions.append(
            "Code context selection was used as targeting guidance and project-memory retrieval, not as an execution gate."
        )

        unresolved_questions = list(selection.context_gaps)
        if not candidate_files:
            unresolved_questions.append(
                "No concrete candidate files were selected; execution planning may need to infer new-file creation from task intent."
            )

        context = CodeExecutorInput(
            task_id=task.id,
            project_id=task.project_id,
            title=task.title,
            description=task.description,
            objective=task.objective,
            acceptance_criteria=task.acceptance_criteria,
            technical_constraints=task.technical_constraints,
            out_of_scope=task.out_of_scope,
            execution_goal=execution_goal,
            repo_root=str(prepared_workspace.workspace_dir),
            relevant_decisions=relevant_decisions,
            candidate_modules=selection.candidate_modules,
            candidate_files=candidate_files,
            primary_targets=primary_targets,
            related_files=related_files,
            reference_files=reference_files,
            related_test_files=related_test_files,
            relevant_symbols=selection.relevant_symbols,
            unresolved_questions=unresolved_questions,
            selection_rationale=selection.selection_rationale,
            selection_confidence=selection.confidence_score,
        )

        artifact_id = self._persist_phase_artifact(
            task=task,
            artifact_type=CODE_EXECUTOR_CONTEXT_ARTIFACT_TYPE,
            payload_json=context.model_dump_json(indent=2),
        )
        context.relevant_decisions.append(
            f"Structured execution context persisted as artifact_id={artifact_id}."
        )

        return context

    def _build_file_context(
        self,
        *,
        workspace_dir,
        path: str,
        role: str,
    ) -> CodeFileContext:
        content: str | None = None
        summary: str | None = None

        if self.workspace_runtime.file_exists(workspace_dir, path):
            try:
                content = self.workspace_runtime.read_file(workspace_dir, path)
                summary = f"Existing workspace file selected as {role}."
            except WorkspaceRuntimeError:
                content = None
                summary = f"{role} file exists but could not be read safely."
        else:
            summary = f"{role} file does not exist yet and may be created if needed."

        return CodeFileContext(
            path=path,
            role=role,
            content=content,
            summary=summary,
            relevant_snippets=[],
            symbols=[],
        )

    @staticmethod
    def _dedupe_keep_order(paths: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            result.append(path)

        return result

    def build_working_set(
        self,
        context: CodeExecutorInput,
        prepared_workspace: PreparedWorkspace,
    ) -> CodeWorkingSet:
        """
        Materialize the semantic roles already decided by the code context selector.

        This method must not:
        - invent its own ranking
        - slice candidate_files arbitrarily
        - collapse targets and supporting files into one flat bag

        candidate_files remains useful as a broader pool for downstream planning,
        but the working set is built from the explicit role-based fields.
        """
        workspace_dir = prepared_workspace.workspace_dir
        file_contexts: list[CodeFileContext] = []
        seen_paths: set[str] = set()

        target_files = self._dedupe_keep_order(context.primary_targets)
        related_files = self._dedupe_keep_order(context.related_files)
        reference_files = self._dedupe_keep_order(context.reference_files)
        test_files = self._dedupe_keep_order(context.related_test_files)

        def add_paths(paths: list[str], role: str) -> None:
            for path in paths:
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                file_contexts.append(
                    self._build_file_context(
                        workspace_dir=workspace_dir,
                        path=path,
                        role=role,
                    )
                )

        add_paths(target_files, CODE_FILE_ROLE_TARGET)
        add_paths(related_files, CODE_FILE_ROLE_RELATED)
        add_paths(reference_files, CODE_FILE_ROLE_REFERENCE)
        add_paths(test_files, CODE_FILE_ROLE_TEST)

        working_set = CodeWorkingSet(
            repo_root=str(workspace_dir),
            target_files=target_files,
            related_files=related_files,
            reference_files=reference_files,
            test_files=test_files,
            files=file_contexts,
            repo_guidance=[
                "Use only relative project paths inside the isolated workspace.",
                "Prefer minimal and scoped file changes.",
                "Use the explicit target/related/reference/test split from the resolved code context.",
                "Do not expand task scope beyond the requested objective.",
                "If the selected file lists are sparse, infer the smallest valid file surface from task intent rather than treating candidate_files as equivalent targets.",
            ],
        )

        self._persist_phase_artifact(
            task=self._task_stub_from_context(context),
            artifact_type=CODE_EXECUTOR_WORKING_SET_ARTIFACT_TYPE,
            payload_json=working_set.model_dump_json(indent=2),
        )

        return working_set

    def build_edit_plan(
        self,
        task: Task,
        context: CodeExecutorInput,
        working_set: CodeWorkingSet,
    ) -> CodeFileEditPlan:
        try:
            plan_response = plan_file_edits(
                context=context,
                working_set=working_set,
            )
        except CodeExecutorClientError as exc:
            raise CodeExecutorInternalError(
                message=f"Failed to obtain file edit plan from model: {str(exc)}",
                failure_code="code_edit_plan_model_failed",
            ) from exc

        if plan_response.decision == CODE_GENERATION_DECISION_REJECT:
            raise CodeExecutorRejectedError(
                message=plan_response.rejection_reason or "The model rejected the task.",
                failure_code="model_rejected_execution",
                remaining_scope=plan_response.remaining_scope,
                blockers_found=plan_response.blockers_found,
            )

        if plan_response.decision != CODE_GENERATION_DECISION_PROCEED:
            raise CodeExecutorInternalError(
                message=f"Unsupported plan decision '{plan_response.decision}'.",
                failure_code="unsupported_plan_decision",
            )

        if not plan_response.planned_changes:
            raise CodeExecutorRejectedError(
                message="The model did not produce any concrete file changes.",
                failure_code="empty_plan_from_model",
                remaining_scope="Task requires richer execution context or a narrower objective.",
                blockers_found="Model returned an empty edit plan.",
            )

        if len(plan_response.planned_changes) > 10:
            raise CodeExecutorRejectedError(
                message="Task scope is too broad for a safe code execution pass.",
                failure_code="scope_too_broad",
                remaining_scope="Task should be re-atomized or constrained before code execution.",
                blockers_found="Planned file surface is too large for a single atomic task.",
            )

        planned_changes = [
            PlannedFileChange(
                path=item.path,
                action=item.action,
                purpose=item.purpose,
                rationale=item.rationale,
            )
            for item in plan_response.planned_changes
        ]

        edit_plan = CodeFileEditPlan(
            task_id=context.task_id,
            summary=plan_response.summary,
            planned_changes=planned_changes,
            assumptions=plan_response.assumptions,
            local_risks=plan_response.local_risks,
            notes=plan_response.notes,
        )

        self._persist_phase_artifact(
            task=task,
            artifact_type=CODE_EXECUTOR_EDIT_PLAN_ARTIFACT_TYPE,
            payload_json=edit_plan.model_dump_json(indent=2),
        )

        return edit_plan

    def apply_changes(
        self,
        task: Task,
        prepared_workspace: PreparedWorkspace,
        context: CodeExecutorInput,
        working_set: CodeWorkingSet,
        plan: CodeFileEditPlan,
    ) -> WorkspaceChangeSet:
        workspace_dir = prepared_workspace.workspace_dir

        try:
            generation_response = generate_file_contents(
                context=context,
                working_set=working_set,
                edit_plan=plan,
            )
        except CodeExecutorClientError as exc:
            raise CodeExecutorInternalError(
                message=f"Failed to generate file contents from model: {str(exc)}",
                failure_code="code_file_generation_model_failed",
            ) from exc

        planned_by_path = {item.path: item for item in plan.planned_changes}
        generated_by_path = {item.path: item for item in generation_response.generated_files}

        if set(generated_by_path.keys()) != set(planned_by_path.keys()):
            raise CodeExecutorInternalError(
                message=(
                    "Generated files do not match the approved edit plan. "
                    f"planned={sorted(planned_by_path.keys())}, "
                    f"generated={sorted(generated_by_path.keys())}"
                ),
                failure_code="generated_files_mismatch_plan",
            )

        try:
            for path, generated_file in generated_by_path.items():
                planned_change = planned_by_path[path]

                if generated_file.action != planned_change.action:
                    raise CodeExecutorInternalError(
                        message=(
                            f"Generated action mismatch for path '{path}'. "
                            f"planned={planned_change.action}, generated={generated_file.action}"
                        ),
                        failure_code="generated_action_mismatch_plan",
                    )

                if generated_file.action == CODE_GENERATION_ACTION_CREATE:
                    if self.workspace_runtime.file_exists(workspace_dir, path):
                        self.workspace_runtime.write_file(
                            workspace_dir,
                            path,
                            generated_file.content,
                        )
                    else:
                        self.workspace_runtime.create_file(
                            workspace_dir,
                            path,
                            generated_file.content,
                        )

                elif generated_file.action == CODE_GENERATION_ACTION_MODIFY:
                    self.workspace_runtime.write_file(
                        workspace_dir,
                        path,
                        generated_file.content,
                    )

                else:
                    raise CodeExecutorInternalError(
                        message=f"Unsupported generated file action '{generated_file.action}'.",
                        failure_code="unsupported_generated_file_action",
                    )

            return self.workspace_runtime.collect_changes(
                project_id=prepared_workspace.project_id,
                execution_run_id=prepared_workspace.execution_run_id,
                domain_name=prepared_workspace.domain_name,
            )
        except WorkspaceRuntimeError as exc:
            raise CodeExecutorInternalError(
                message=f"Failed while applying workspace changes: {str(exc)}",
                failure_code="workspace_change_application_failed",
            ) from exc

    def build_journal(
        self,
        context: CodeExecutorInput,
        prepared_workspace: PreparedWorkspace,
        working_set: CodeWorkingSet,
        plan: CodeFileEditPlan,
        changes: WorkspaceChangeSet,
        execution_status: str,
    ) -> ExecutionJournal:
        return ExecutionJournal(
            task_id=context.task_id,
            summary=(
                f"Code executor completed its operational phase for task {context.task_id} "
                f"with status '{execution_status}' in workspace "
                f"run={prepared_workspace.execution_run_id}."
            ),
            local_decisions=[
                "Execution scope was limited to the selected working set and explicit task intent.",
                "Repository targeting was resolved from cumulative project memory plus task-related context.",
                "Working set roles were materialized directly from the resolved code context.",
                "Changes were applied only inside the isolated workspace.",
            ],
            claimed_completed_scope=(
                "Resolved code context, working set, model-generated edit plan, and workspace change set were produced."
                if execution_status == CODE_EXECUTION_STATUS_AWAITING_VALIDATION
                else None
            ),
            claimed_remaining_scope=None,
            encountered_uncertainties=context.unresolved_questions,
            notes_for_validator=[
                "Validate whether the selected context and produced changes actually satisfy the task.",
                "Use the context gaps as caution signals, not as automatic failure reasons.",
                "Check whether new-file creation was justified when repository targeting was sparse.",
            ],
        )

    def execute(self, task: Task, execution_run_id: int) -> CodeExecutorResult:
        try:
            prepared_workspace = self.prepare_workspace(task, execution_run_id)
            context = self.resolve_context(
                task=task,
                prepared_workspace=prepared_workspace,
            )
            working_set = self.build_working_set(context, prepared_workspace)
            plan = self.build_edit_plan(task, context, working_set)
            changes = self.apply_changes(
                task=task,
                prepared_workspace=prepared_workspace,
                context=context,
                working_set=working_set,
                plan=plan,
            )

            result = CodeExecutorResult(
                task_id=task.id,
                execution_status=CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
                input=context,
                working_set=working_set,
                edit_plan=plan,
                workspace_changes=changes,
                journal=self.build_journal(
                    context=context,
                    prepared_workspace=prepared_workspace,
                    working_set=working_set,
                    plan=plan,
                    changes=changes,
                    execution_status=CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
                ),
                output_snapshot="code_executor_result_created",
            )

            artifact_id = self._persist_phase_artifact(
                task=task,
                artifact_type=CODE_EXECUTOR_RESULT_ARTIFACT_TYPE,
                payload_json=result.model_dump_json(indent=2),
            )
            result.journal.notes_for_validator.append(
                f"Structured executor result persisted as artifact_id={artifact_id}."
            )

            return result

        except CodeExecutorRejectedError:
            raise
        except CodeExecutorInternalError:
            raise
        except Exception as exc:
            raise CodeExecutorInternalError(
                message=f"Code executor failed unexpectedly: {str(exc)}",
                failure_code="code_executor_internal_error",
            ) from exc


__all__ = [
    "BaseCodeExecutor",
    "LocalCodeExecutor",
    "CodeExecutorError",
    "CodeExecutorRejectedError",
    "CodeExecutorInternalError",
]