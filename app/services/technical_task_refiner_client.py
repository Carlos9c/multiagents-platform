from pydantic import ValidationError

from app.schemas.technical_task_refiner import TechnicalTaskRefinementOutput
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

TECHNICAL_TASK_REFINER_SYSTEM_PROMPT = prompt_loader.get("technical_task_refiner")


def build_refiner_user_prompt(
    *,
    project_name: str,
    project_description: str,
    parent_task_title: str,
    parent_task_description: str,
    parent_task_summary: str,
    parent_task_objective: str,
    parent_task_type: str,
    parent_task_implementation_notes: str,
    parent_task_acceptance_criteria: str,
    parent_task_technical_constraints: str,
    parent_task_out_of_scope: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "technical_task_refiner",
        "main",
        {
            "project_name": project_name,
            "project_description": project_description,
            "parent_task_title": parent_task_title,
            "parent_task_description": parent_task_description,
            "parent_task_summary": parent_task_summary,
            "parent_task_objective": parent_task_objective,
            "parent_task_type": parent_task_type,
            "parent_task_implementation_notes": parent_task_implementation_notes,
            "parent_task_acceptance_criteria": parent_task_acceptance_criteria,
            "parent_task_technical_constraints": parent_task_technical_constraints,
            "parent_task_out_of_scope": parent_task_out_of_scope,
        },
    )
    return f"""
Project name: {project_name}
Project description: {project_description}

Parent high-level task:
- title: {parent_task_title}
- description: {parent_task_description}
- summary: {parent_task_summary}
- objective: {parent_task_objective}
- task_type: {parent_task_type}
- implementation_notes: {parent_task_implementation_notes}
- acceptance_criteria: {parent_task_acceptance_criteria}
- technical_constraints: {parent_task_technical_constraints}
- out_of_scope: {parent_task_out_of_scope}

Important:
- Create refined tasks only.
- Do not jump to atomic file-by-file actions.
- Preserve the intent of the parent task.
- Make the output suitable for a later Atomic Task Generator.
- Do not decide the final executor at refined level.
- A refined task may later split into atomic work for different executors.
- Respect the real domain of the task instead of forcing it into software-only work.
- Do not assume internal platform entities are the domain entities of the project unless explicitly required.
- If the task belongs to documentation, onboarding, design, research, media, content, or another non-code domain, keep the refinement in that domain.

Completeness reminder:
- before finalizing, check whether the parent task has been decomposed into a complete enough refined set
- include testing, documentation, setup, or clarification subtasks only when they are genuinely relevant to the parent task
- do not add filler subtasks just to satisfy a pattern
""".strip()


def build_refiner_retry_prompt(
    *,
    project_name: str,
    project_description: str,
    parent_task_title: str,
    validation_error: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "technical_task_refiner",
        "retry",
        {
            "project_name": project_name,
            "project_description": project_description,
            "parent_task_title": parent_task_title,
            "validation_error": validation_error,
        },
    )
    return f"""
Project name: {project_name}
Project description: {project_description}
Parent high-level task title: {parent_task_title}

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only refined tasks
- include proposed_solution
- include implementation_steps as a list
- include tests_required as a list
- do not include atomic file-level instructions
- do not include extra keys
- do not decide the final executor at refined level
- refined tasks may later split into atomic tasks for different executors
- do not assume every project or task is purely software
- respect the actual domain of the parent task
- do not assume internal platform entities are the project domain model unless explicitly required

Completeness reminder:
- silently self-check whether the refinement is complete enough for the parent task
- add missing meaningful subtasks when necessary
- do not force documentation or onboarding if they are not natural parts of the refinement
- avoid filler tasks
""".strip()


def call_technical_task_refiner_model(
    *,
    project_name: str,
    project_description: str,
    parent_task_title: str,
    parent_task_description: str,
    parent_task_summary: str,
    parent_task_objective: str,
    parent_task_type: str,
    parent_task_implementation_notes: str,
    parent_task_acceptance_criteria: str,
    parent_task_technical_constraints: str,
    parent_task_out_of_scope: str,
) -> TechnicalTaskRefinementOutput:
    provider = get_llm_provider()
    first_user_prompt = build_refiner_user_prompt(
        project_name=project_name,
        project_description=project_description,
        parent_task_title=parent_task_title,
        parent_task_description=parent_task_description,
        parent_task_summary=parent_task_summary,
        parent_task_objective=parent_task_objective,
        parent_task_type=parent_task_type,
        parent_task_implementation_notes=parent_task_implementation_notes,
        parent_task_acceptance_criteria=parent_task_acceptance_criteria,
        parent_task_technical_constraints=parent_task_technical_constraints,
        parent_task_out_of_scope=parent_task_out_of_scope,
    )

    raw = provider.generate_structured(
        system_prompt=TECHNICAL_TASK_REFINER_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="technical_task_refinement_output",
        json_schema=TechnicalTaskRefinementOutput.model_json_schema(),
    )

    try:
        return TechnicalTaskRefinementOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_refiner_retry_prompt(
            project_name=project_name,
            project_description=project_description,
            parent_task_title=parent_task_title,
            validation_error=str(exc),
        )
        raw_retry = provider.generate_structured(
            system_prompt=TECHNICAL_TASK_REFINER_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="technical_task_refinement_output",
            json_schema=TechnicalTaskRefinementOutput.model_json_schema(),
        )
        return TechnicalTaskRefinementOutput.model_validate(raw_retry)
