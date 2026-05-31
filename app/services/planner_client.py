from __future__ import annotations

from pydantic import ValidationError

from app.schemas.planner import PlannerOutput
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_planning_trace

PLANNER_SYSTEM_PROMPT = prompt_loader.get("planner")


def build_planner_user_prompt(
    project_name: str,
    project_description: str,
    runtime_context: str | None = None,
) -> str:
    prompt_loader.validate_builder_inputs(
        "planner",
        "main",
        {
            "project_name": project_name,
            "project_description": project_description,
            "runtime_context": runtime_context,
        },
    )
    runtime_section = f"\n{runtime_context}\n" if runtime_context else ""
    return f"""
Project name: {project_name}
Project description: {project_description}
{runtime_section}
Important:
- Plan the user's project, not the internal implementation of the orchestration platform.
- Think in terms of real project deliverables and workstreams.
- Do not assume the project is only software unless the description clearly indicates that.
- If the project is software-focused, produce software-oriented high-level tasks.
- If the project includes documentation, setup, research, design, media, content, process, onboarding, or mixed deliverables, preserve them as first-class tasks when they are genuinely part of the requested project.

Execution-model reminder:
- produce high_level tasks only
- do not produce refined tasks
- do not produce atomic tasks
- high_level tasks will later be decomposed directly into atomic tasks
- therefore each high_level task must be bounded, clear, and decomposition-friendly
- do not over-shrink tasks into pseudo-atomic items

Executor reminder:
- do not decide the final executor for high_level tasks
- stay focused on deliverables and approach, not executor binding

Domain reminder:
- internal platform entities like Project, Task, Artifact, and ExecutionRun are orchestration concepts
- do not assume they are the domain model of the requested project
- only couple to platform internals if the user explicitly asks for that

Completeness reminder:
- before finalizing, check whether the plan is missing any important workstream for this specific kind of project
- include documentation, onboarding, testing, design, requirements, setup, or other support tasks only when they are genuinely relevant
- do not force generic task categories if they do not fit the project

Quality reminder:
- avoid vague buckets like "do implementation"
- avoid pseudo-atomic tasks like single-file or single-endpoint work
- make each task meaningful, bounded, and useful for later direct atomic decomposition
""".strip()


def build_planner_retry_prompt(
    project_name: str,
    project_description: str,
    validation_error: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "planner",
        "retry",
        {
            "project_name": project_name,
            "project_description": project_description,
            "validation_error": validation_error,
        },
    )
    return f"""
Project name: {project_name}
Project description: {project_description}

Your previous answer was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important:
- generate high_level tasks only
- do not produce refined tasks
- do not produce atomic tasks
- do not assume every project is purely software unless clearly stated
- preserve documentation, onboarding, setup, research, design, media, process, and other real deliverables when they are truly part of the project
- do not decide the final executor for high_level tasks
- do not assume internal platform entities are the domain model of the requested project
- do not force the project to reuse Project, Task, Artifact, or ExecutionRun as business entities

Direct decomposition reminder:
- the resulting high_level tasks must be suitable for later direct atomic decomposition
- keep them bounded and clear
- do not collapse the project into vague buckets
- do not over-fragment into pseudo-atomic work

Completeness reminder:
- silently self-check whether the plan is complete enough for the project type
- if a critical area is missing, add it
- do not force documentation or onboarding when they are not natural deliverables
- if the project is software-oriented, implementation should usually appear and documentation/testing/onboarding should be considered when relevant

Task type reminder:
- use the task type that best matches the primary nature of each task
- keep the task mix contextual rather than formulaic
""".strip()


_MAX_ANALYSIS_FILES = 60


def _build_evolutionary_context_section(analysis: object) -> str:
    """Render a condensed codebase-context block for the evolutionary planner prompt."""
    lines = [
        "EXISTING CODEBASE CONTEXT:",
        f"Project summary: {analysis.project_summary}",
        f"Main technologies: {', '.join(analysis.main_technologies) or 'unknown'}",
        f"Entry points: {', '.join(analysis.entry_points) or 'none identified'}",
        f"Total files analysed: {analysis.total_files}",
        "",
        "File catalogue (up to 60 files):",
    ]
    for fa in analysis.files[:_MAX_ANALYSIS_FILES]:
        lang = f", {fa.language}" if fa.language else ""
        lines.append(f"  {fa.path} ({fa.file_type}{lang}): {fa.summary}")
    return "\n".join(lines)


def build_evolutionary_planner_user_prompt(
    project_name: str,
    project_description: str,
    analysis: object,
    runtime_context: str | None = None,
) -> str:
    context_section = _build_evolutionary_context_section(analysis)
    prompt_loader.validate_builder_inputs(
        "planner",
        "evolutionary",
        {
            "project_name": project_name,
            "project_description": project_description,
            "codebase_analysis_context": context_section,
            "runtime_context": runtime_context,
        },
    )
    runtime_section = f"\n{runtime_context}\n" if runtime_context else ""
    return f"""
{context_section}

---

NEW OBJECTIVE:
Project name: {project_name}
Project description: {project_description}
{runtime_section}

Important:
- Plan the next iteration of work given the existing codebase above.
- The plan must complement or continue what already exists — do not re-plan work that is already done.
- If the new objective requires changes to existing components, name those components explicitly.
- If the existing codebase is incomplete or missing key areas, include tasks to address them.
- Preserve the real nature of the project instead of forcing it into a narrow implementation-only plan.
- Do not assume the project is only software unless the description clearly indicates that.

Execution-model reminder:
- produce high_level tasks only
- do not produce refined tasks
- do not produce atomic tasks
- high_level tasks will later be decomposed directly into atomic tasks
- therefore each high_level task must be bounded, clear, and decomposition-friendly
- do not over-shrink tasks into pseudo-atomic items

Executor reminder:
- do not decide the final executor for high_level tasks
- stay focused on deliverables and approach, not executor binding

Completeness reminder:
- before finalizing, check whether the plan is missing any important workstream for this specific kind of project
- include documentation, onboarding, testing, design, requirements, setup, or other support tasks only when they are genuinely relevant
- do not force generic task categories if they do not fit the project

Quality reminder:
- avoid vague buckets like "do implementation"
- avoid pseudo-atomic tasks like single-file or single-endpoint work
- make each task meaningful, bounded, and useful for later direct atomic decomposition
""".strip()


def call_planner_model(
    project_name: str,
    project_description: str,
    *,
    runtime_context: str | None = None,
    project_id: int | None = None,
) -> PlannerOutput:
    provider = get_llm_provider()
    first_user_prompt = build_planner_user_prompt(
        project_name=project_name,
        project_description=project_description,
        runtime_context=runtime_context,
    )

    raw = provider.generate_structured(
        system_prompt=PLANNER_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="planner_output",
        json_schema=PlannerOutput.model_json_schema(),
    )

    try:
        result = PlannerOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_planner_retry_prompt(
            project_name=project_name,
            project_description=project_description,
            validation_error=str(exc),
        )
        raw_retry = provider.generate_structured(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="planner_output",
            json_schema=PlannerOutput.model_json_schema(),
        )
        result = PlannerOutput.model_validate(raw_retry)

    if project_id is not None:
        append_planning_trace(
            project_id=project_id,
            entry={
                "agent": "planner",
                "call_type": "initial",
                "project_id": project_id,
                "inputs": {
                    "project_name": project_name,
                    "project_description": project_description,
                },
                "reasoning": result.plan_summary,
                "tasks_produced_count": len(result.tasks),
                "task_titles": [t.title for t in result.tasks],
                "output_snapshot": result.model_dump(),
            },
        )

    return result


def call_evolutionary_planner_model(
    project_name: str,
    project_description: str,
    codebase_analysis: object,
    *,
    runtime_context: str | None = None,
    project_id: int | None = None,
) -> PlannerOutput:
    provider = get_llm_provider()
    first_user_prompt = build_evolutionary_planner_user_prompt(
        project_name=project_name,
        project_description=project_description,
        analysis=codebase_analysis,
        runtime_context=runtime_context,
    )

    raw = provider.generate_structured(
        system_prompt=PLANNER_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="planner_output",
        json_schema=PlannerOutput.model_json_schema(),
    )

    try:
        result = PlannerOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_planner_retry_prompt(
            project_name=project_name,
            project_description=project_description,
            validation_error=str(exc),
        )
        raw_retry = provider.generate_structured(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="planner_output",
            json_schema=PlannerOutput.model_json_schema(),
        )
        result = PlannerOutput.model_validate(raw_retry)

    if project_id is not None:
        append_planning_trace(
            project_id=project_id,
            entry={
                "agent": "planner",
                "call_type": "evolutionary",
                "project_id": project_id,
                "inputs": {
                    "project_name": project_name,
                    "project_description": project_description,
                },
                "reasoning": result.plan_summary,
                "tasks_produced_count": len(result.tasks),
                "task_titles": [t.title for t in result.tasks],
                "output_snapshot": result.model_dump(),
            },
        )

    return result
