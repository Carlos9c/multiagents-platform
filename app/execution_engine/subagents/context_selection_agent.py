from __future__ import annotations

import json

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
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema

HISTORICAL_TASK_SELECTION_SYSTEM_PROMPT = """
You are a historical task selector for atomic task execution.

Your job is to decide which COMPLETED historical tasks and their associated completion runs
must enter the execution context for the CURRENT atomic task.

You are not selecting files directly.
You are selecting previously completed task/run pairs that provide necessary operational context.

Core rules:
- Selection is BINARY: a historical task/run pair either enters or does not enter.
- Do not output maybe / possibly / optional categories.
- Select only from the catalog provided in the prompt.
- For every selected item, return exactly one valid selection_rule and one concrete selection_reason.
- The selected execution_run_id must be one of the runs provided in the catalog.
- Return ONLY JSON matching the provided schema.

Valid selection rules:
- same_functional_surface:
  the historical task resolved a part of the system that the current task needs to extend,
  modify, or use as a base.
- same_work_strategy:
  the historical task implemented a solution very similar to what the current task now requires,
  even if the exact files are not identical.
- direct_historical_dependency:
  the current task depends directly on the result of that previous task.
- required_operational_context:
  without understanding what that historical task resolved, the executor would face a high risk
  of inconsistency, duplication, or regression.

Selection philosophy:
- Select only tasks that are genuinely necessary as execution context.
- Do not select tasks for superficial thematic similarity.
- Prefer operational necessity over broad recall.
- The current task is always atomic. Historical tasks are only support context.

Coherence expectations:
- Consider repository structure, naming consistency, methodology, implementation conventions,
  validation practices, and design consistency when those signals are visible in:
  - the current task
  - the project context excerpt
  - the historical task catalog

Important:
- The output will be used to build the final ExecutionRequest deterministically.
- Therefore, be strict, concrete, and conservative.
""".strip()


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
        "task_type": entry.task_type,
        "executor_type": entry.executor_type,
        "run_summary": entry.run_summary,
        "completed_scope": entry.completed_scope,
        "validation_notes": entry.validation_notes,
        "changed_files": entry.changed_files,
        "files_read": entry.files_read,
    }


def _build_historical_task_selection_user_prompt(
    *,
    current_task: Task,
    catalog: list[HistoricalTaskCatalogEntry],
    project_name: str,
    project_description: str,
    project_context_excerpt: str | None = None,
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

    return f"""
Project name: {project_name}
Project description: {project_description}

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


def _build_historical_task_selection_retry_prompt(
    *,
    project_name: str,
    current_task_title: str,
    validation_error: str,
) -> str:
    return f"""
Project name: {project_name}
Current atomic task title: {current_task_title}

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

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


def call_context_selection_model(
    *,
    current_task: Task,
    project: Project,
    context_input: ContextBuilderResult,
) -> HistoricalTaskSelectionResult:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(HistoricalTaskSelectionResult.model_json_schema())

    first_user_prompt = _build_historical_task_selection_user_prompt(
        current_task=current_task,
        catalog=context_input.completed_task_catalog,
        project_name=project.name,
        project_description=project.description or project.name,
        project_context_excerpt=context_input.project_context_excerpt,
    )

    raw = provider.generate_structured(
        system_prompt=HISTORICAL_TASK_SELECTION_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="historical_task_selection_result",
        json_schema=strict_schema,
    )

    try:
        result = HistoricalTaskSelectionResult.model_validate(raw)
        return _validate_historical_task_selection(
            result,
            catalog=context_input.completed_task_catalog,
        )
    except (ValidationError, SubagentRejectedStepError) as exc:
        retry_user_prompt = _build_historical_task_selection_retry_prompt(
            project_name=project.name,
            current_task_title=current_task.title,
            validation_error=str(exc),
        )

        raw_retry = provider.generate_structured(
            system_prompt=HISTORICAL_TASK_SELECTION_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="historical_task_selection_result",
            json_schema=strict_schema,
        )

        result_retry = HistoricalTaskSelectionResult.model_validate(raw_retry)
        return _validate_historical_task_selection(
            result_retry,
            catalog=context_input.completed_task_catalog,
        )


class ContextSelectionAgent(BaseSubagent):
    name = "context_selection_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def execute_step(
        self,
        *,
        db: Session,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ):
        current_task: Task | None = db.get(Task, request.task_id)
        if current_task is None:
            raise SubagentRejectedStepError(
                f"Task {request.task_id} not found during context selection."
            )

        project: Project | None = db.get(Project, request.project_id)
        if project is None:
            raise SubagentRejectedStepError(
                f"Project {request.project_id} not found during context selection."
            )

        context_input = build_context_selection_input(
            db=db,
            current_task=current_task,
        )

        if not context_input.should_invoke_context_selection_agent:
            state.set_historical_task_selection(
                HistoricalTaskSelectionResult(selected_task_runs=[])
            )

            enriched_request = adapt_execution_request(
                db=db,
                request=state.execution_request,
                context_selection_result=state.historical_task_selection,
            )
            state.replace_execution_request(enriched_request)

            state.evidence.add_note(
                message="No completed historical tasks available. Context selection skipped.",
                producer=self.name,
            )
            state.add_note("No completed historical tasks available. Context selection skipped.")
            state.mark_context_selected()
            return state

        selection_result = call_context_selection_model(
            current_task=current_task,
            project=project,
            context_input=context_input,
        )
        state.set_historical_task_selection(selection_result)

        enriched_request = adapt_execution_request(
            db=db,
            request=state.execution_request,
            context_selection_result=selection_result,
        )
        state.replace_execution_request(enriched_request)

        selected_count = len(selection_result.selected_task_runs)

        state.evidence.add_note(
            message=f"Historical context selection completed. selected_task_runs={selected_count}.",
            producer=self.name,
        )
        state.add_note("Historical context selection completed.")
        state.mark_context_selected()
        return state
