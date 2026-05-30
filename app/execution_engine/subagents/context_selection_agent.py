from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.context_selection import (
    ContextBuilderResult,
    HistoricalTaskCatalogEntry,
    HistoricalTaskSelectionResult,
)
from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.request_adapter import adapt_execution_request
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import (
    BaseSubagent,
    SubagentRejectedStepError,
)
from app.execution_engine.tools.context_builder_tool import (
    build_context_selection_input,
)
from app.models.project import Project
from app.models.task import Task
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_execution_trace

logger = logging.getLogger(__name__)

HISTORICAL_TASK_SELECTION_SYSTEM_PROMPT = prompt_loader.get("context_selection_agent")


def _task_to_prompt_payload(task: Task) -> dict:
    return {
        "task_id": task.id,
        "title": task.title,
        "description": task.description,
        "summary": task.summary,
        "objective": task.objective,
        "proposed_solution": task.proposed_solution,
        "implementation_notes": task.implementation_notes,
        "implementation_steps": task.implementation_steps,
        "acceptance_criteria": task.acceptance_criteria,
        "tests_required": task.tests_required,
        "technical_constraints": task.technical_constraints,
        "out_of_scope": task.out_of_scope,
        "task_type": task.task_type,
        "executor_type": task.executor_type,
    }


def _catalog_entry_to_prompt_payload(
    entry: HistoricalTaskCatalogEntry,
) -> dict:
    return {
        "task_id": entry.task_id,
        "execution_run_id": entry.execution_run_id,
        "title": entry.title,
        "description": entry.description,
        "summary": entry.summary,
        "objective": entry.objective,
        "acceptance_criteria": entry.acceptance_criteria,
        "proposed_solution": entry.proposed_solution,
        "task_type": entry.task_type,
        "executor_type": entry.executor_type,
        "run_summary": entry.run_summary,
        "completed_scope": entry.completed_scope,
        "validation_notes": entry.validation_notes,
        "changed_files": entry.changed_files,
        "files_read": entry.files_read,
    }


def _build_historical_task_selection_base_prompt(
    *,
    current_task: Task,
    catalog: list[HistoricalTaskCatalogEntry],
    project_name: str,
    project_description: str,
    project_context_excerpt: str | None = None,
    codebase_analysis_excerpt: str | None = None,
) -> str:
    rules = [
        {
            "rule": "same_functional_surface",
            "meaning": (
                "The historical task resolved a part of the system that the current task "
                "needs to extend, modify, or use as a base."
            ),
        },
        {
            "rule": "same_work_strategy",
            "meaning": (
                "The historical task implemented a solution very similar to what the current "
                "task now requires, even if the exact files are not identical."
            ),
        },
        {
            "rule": "direct_historical_dependency",
            "meaning": ("The current task depends directly on the result of that previous task."),
        },
        {
            "rule": "required_operational_context",
            "meaning": (
                "Without understanding what that historical task resolved, the executor would "
                "face a high risk of inconsistency, duplication, or regression."
            ),
        },
    ]

    codebase_section = (
        f"Codebase structure (static analysis snapshot):\n{codebase_analysis_excerpt}"
        if codebase_analysis_excerpt
        else "Codebase structure: No static analysis available."
    )

    return f"""
Project name: {project_name}
Project description: {project_description}

{codebase_section}

Current atomic task:
{json.dumps(_task_to_prompt_payload(current_task), ensure_ascii=False, indent=2)}

Project context excerpt:
{project_context_excerpt or "None"}

Valid selection rules:
{json.dumps(rules, ensure_ascii=False, indent=2)}

Completed historical task catalog:
{json.dumps([_catalog_entry_to_prompt_payload(entry) for entry in catalog], ensure_ascii=False, indent=2)}

Return ONLY JSON with this exact shape:
{{
  "selected_task_runs": [
    {{
      "task_id": 123,
      "execution_run_id": 456,
      "selection_rule": "same_functional_surface",
      "selection_reason": "Concrete operational reason"
    }}
  ]
}}

Important:
- Select only task/run pairs that must enter execution context.
- Selection is binary: enter or do not enter.
- Do not return any extra keys.
- Do not invent task ids or execution run ids.
- Do not select tasks just because they are broadly similar in topic.
- Prefer concrete operational necessity.
""".strip()


def _build_historical_task_selection_user_prompt(
    *,
    current_task: Task,
    catalog: list[HistoricalTaskCatalogEntry],
    project_name: str,
    project_description: str,
    project_context_excerpt: str | None = None,
    codebase_analysis_excerpt: str | None = None,
) -> str:
    prompt_loader.validate_builder_inputs(
        "context_selection_agent",
        "main",
        {
            "current_task": current_task,
            "historical_task_catalog": catalog,
            "project_name": project_name,
            "project_description": project_description,
            "project_context_excerpt": project_context_excerpt,
            "codebase_analysis_excerpt": codebase_analysis_excerpt,
        },
    )
    return _build_historical_task_selection_base_prompt(
        current_task=current_task,
        catalog=catalog,
        project_name=project_name,
        project_description=project_description,
        project_context_excerpt=project_context_excerpt,
        codebase_analysis_excerpt=codebase_analysis_excerpt,
    )


def _build_historical_task_selection_retry_prompt(
    *,
    current_task: Task,
    catalog: list[HistoricalTaskCatalogEntry],
    project_name: str,
    project_description: str,
    project_context_excerpt: str | None = None,
    validation_error: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "context_selection_agent",
        "retry",
        {
            "project_name": project_name,
            "project_description": project_description,
            "current_task": current_task,
            "historical_task_catalog": catalog,
            "project_context_excerpt": project_context_excerpt,
            "validation_error": validation_error,
        },
    )
    return f"""
Project name: {project_name}
Project description: {project_description}

Current atomic task:
{json.dumps(_task_to_prompt_payload(current_task), ensure_ascii=False, indent=2)}

Project context excerpt:
{project_context_excerpt or "None"}

Completed historical task catalog:
{json.dumps([_catalog_entry_to_prompt_payload(entry) for entry in catalog], ensure_ascii=False, indent=2)}

Your previous output was invalid.

Validation error:
{validation_error}

Return ONLY JSON with this exact shape:
{{
  "selected_task_runs": [
    {{
      "task_id": 123,
      "execution_run_id": 456,
      "selection_rule": "same_functional_surface",
      "selection_reason": "Concrete operational reason"
    }}
  ]
}}

Important corrections:
- selection is binary: a task/run pair enters or does not enter
- output only selected_task_runs
- every selected item must include:
  - task_id
  - execution_run_id
  - selection_rule
  - selection_reason
- selection_rule must be exactly one of:
  - same_functional_surface
  - same_work_strategy
  - direct_historical_dependency
  - required_operational_context
- do not invent task ids or execution run ids
- select only from the provided catalog
- do not include extra keys
- return only JSON matching the schema
""".strip()


def _validate_historical_task_selection(
    result: HistoricalTaskSelectionResult,
    *,
    catalog: list[HistoricalTaskCatalogEntry],
) -> HistoricalTaskSelectionResult:
    valid_pairs = {(entry.task_id, entry.execution_run_id) for entry in catalog}

    selected_pairs: set[tuple[int, int]] = set()

    for entry in result.selected_task_runs:
        pair = (entry.task_id, entry.execution_run_id)
        if pair not in valid_pairs:
            raise SubagentRejectedStepError(
                "Selected task/run pair is not present in the completed historical task catalog."
            )
        if pair in selected_pairs:
            raise SubagentRejectedStepError("Duplicate task/run pair returned by selector.")
        selected_pairs.add(pair)

    return result


def _write_context_selection_trace(
    *,
    request: ExecutionRequest,
    current_task: "Task",
    context_input: ContextBuilderResult,
    call_type: str,
    selection_result: HistoricalTaskSelectionResult,
) -> None:
    """Append one context_selection_agent entry to execution_trace.jsonl.

    Never raises — trace failures must not interrupt execution.
    """
    catalog = context_input.completed_task_catalog
    catalog_by_id = {e.task_id: e.title for e in catalog}

    selected_tasks = [
        {
            "task_id": s.task_id,
            "title": catalog_by_id.get(s.task_id, "unknown"),
            "selection_reason": s.selection_reason,
        }
        for s in selection_result.selected_task_runs
    ]

    entry = {
        "agent": "context_selection_agent",
        "project_id": request.project_id,
        "task_id": request.task_id,
        "run_id": request.execution_run_id,
        "call_type": call_type,
        "inputs": {
            "current_task_title": current_task.title,
            "current_task_type": current_task.task_type,
            "current_task_objective": current_task.objective,
            "current_task_acceptance_criteria": current_task.acceptance_criteria,
            "relevant_files": list(request.context.relevant_files),
            "key_decisions": list(request.context.key_decisions),
            "candidate_count": len(catalog),
            "candidate_titles": [{"task_id": e.task_id, "title": e.title} for e in catalog],
        },
        "output_snapshot": {
            "selected_count": len(selected_tasks),
            "selected_tasks": selected_tasks,
            "not_selected_count": len(catalog) - len(selected_tasks),
        },
    }
    append_execution_trace(project_id=request.project_id, entry=entry)


class ContextSelectionAgent(BaseSubagent):
    name = "context_selection_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def _call_context_selection_model(
        self,
        *,
        current_task: Task,
        project: Project,
        context_input: ContextBuilderResult,
    ) -> HistoricalTaskSelectionResult:
        first_user_prompt = _build_historical_task_selection_user_prompt(
            current_task=current_task,
            catalog=context_input.completed_task_catalog,
            project_name=project.name,
            project_description=project.description or project.name,
            project_context_excerpt=context_input.project_context_excerpt,
            codebase_analysis_excerpt=context_input.codebase_analysis_excerpt,
        )

        raw = self.runtime.generate_structured(
            system_prompt=HISTORICAL_TASK_SELECTION_SYSTEM_PROMPT,
            user_prompt=first_user_prompt,
            schema_name="historical_task_selection_result",
            json_schema=HistoricalTaskSelectionResult.model_json_schema(),
        )

        try:
            result = HistoricalTaskSelectionResult.model_validate(raw)
            return _validate_historical_task_selection(
                result,
                catalog=context_input.completed_task_catalog,
            )
        except (ValidationError, SubagentRejectedStepError) as exc:
            logger.warning(
                "context_selection_agent_first_attempt_invalid task_id=%s error=%s retrying=true",
                current_task.id,
                str(exc),
            )
            retry_user_prompt = _build_historical_task_selection_retry_prompt(
                current_task=current_task,
                catalog=context_input.completed_task_catalog,
                project_name=project.name,
                project_description=project.description or project.name,
                project_context_excerpt=context_input.project_context_excerpt,
                validation_error=str(exc),
            )

            raw_retry = self.runtime.generate_structured(
                system_prompt=HISTORICAL_TASK_SELECTION_SYSTEM_PROMPT,
                user_prompt=retry_user_prompt,
                schema_name="historical_task_selection_result",
                json_schema=HistoricalTaskSelectionResult.model_json_schema(),
            )

            try:
                result_retry = HistoricalTaskSelectionResult.model_validate(raw_retry)
            except ValidationError as retry_exc:
                logger.error(
                    "context_selection_agent_retry_also_invalid task_id=%s error=%s",
                    current_task.id,
                    str(retry_exc),
                )
                raise SubagentRejectedStepError(
                    f"Invalid historical task selection output after retry: {str(retry_exc)}"
                ) from retry_exc

            return _validate_historical_task_selection(
                result_retry,
                catalog=context_input.completed_task_catalog,
            )

    def execute_step(
        self,
        *,
        db: Session,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if step.subagent_name != self.name:
            raise SubagentRejectedStepError(
                f"{self.name} received a step for subagent '{step.subagent_name}'."
            )

        logger.info(
            "context_selection_agent_started task_id=%s step_id=%s",
            state.execution_request.task_id,
            step.id,
        )

        current_request = state.execution_request

        current_task: Task | None = db.get(Task, current_request.task_id)
        if current_task is None:
            raise SubagentRejectedStepError(
                f"Task {current_request.task_id} not found during context selection."
            )

        project: Project | None = db.get(Project, current_request.project_id)
        if project is None:
            raise SubagentRejectedStepError(
                f"Project {current_request.project_id} not found during context selection."
            )

        context_input = build_context_selection_input(
            db=db,
            current_task=current_task,
        )

        if not context_input.should_invoke_context_selection_agent:
            logger.info(
                "context_selection_agent_skipped task_id=%s reason=no_completed_historical_tasks",
                current_request.task_id,
            )
            empty_selection = HistoricalTaskSelectionResult(selected_task_runs=[])
            state.set_historical_task_selection(empty_selection)

            enriched_request = adapt_execution_request(
                db=db,
                request=current_request,
                context_selection_result=empty_selection,
            )
            state.replace_execution_request(enriched_request)

            _write_context_selection_trace(
                request=current_request,
                current_task=current_task,
                context_input=context_input,
                call_type="skipped",
                selection_result=empty_selection,
            )

            state.evidence.add_note(
                message="No completed historical tasks available. Context selection skipped.",
                producer=self.name,
            )
            state.add_note("No completed historical tasks available. Context selection skipped.")
            state.mark_context_selected()
            return state

        selection_result = self._call_context_selection_model(
            current_task=current_task,
            project=project,
            context_input=context_input,
        )
        state.set_historical_task_selection(selection_result)

        enriched_request = adapt_execution_request(
            db=db,
            request=current_request,
            context_selection_result=selection_result,
        )
        state.replace_execution_request(enriched_request)

        _write_context_selection_trace(
            request=current_request,
            current_task=current_task,
            context_input=context_input,
            call_type="initial",
            selection_result=selection_result,
        )

        selected_count = len(selection_result.selected_task_runs)

        logger.info(
            "context_selection_agent_completed task_id=%s selected_task_runs=%s",
            current_request.task_id,
            selected_count,
        )

        state.evidence.add_note(
            message=f"Historical context selection completed. selected_task_runs={selected_count}.",
            producer=self.name,
        )
        state.add_note("Historical context selection completed.")
        state.mark_context_selected()
        return state
