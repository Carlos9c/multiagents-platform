from pydantic import ValidationError

from app.schemas.technical_task_refiner import TechnicalTaskRefinementOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


TECHNICAL_TASK_REFINER_SYSTEM_PROMPT = """
You are a senior project refinement agent.

Your job is to convert one high-level project task into a small set of refined technical or execution-oriented tasks.
Return ONLY JSON matching the provided schema.

Core mission:
- Transform one high_level task into refined tasks.
- Reduce ambiguity and prepare the task for later decomposition into atomic work.
- Stay at refined level: concrete and actionable, but not yet file-by-file atomic execution.

Platform perspective:
- This platform orchestrates projects through planning, refinement, atomic decomposition, and execution.
- Internal platform entities such as Project, Task, Artifact, and ExecutionRun are orchestration concepts.
- They are not automatically the domain model of the user's project.
- Do NOT assume the refined work must reuse those internal platform entities unless the parent task explicitly requires extending the platform itself.

Executor assignment rule:
- Do NOT decide the final executor for a refined task.
- A refined task may later split into atomic tasks handled by different executors.
- Refined tasks must prepare the work, not prematurely bind it to one executor.
- Focus on decomposition, clarity, deliverables, and validation.

Domain interpretation rules:
- First understand the actual domain of the parent task.
- If the parent task is software-oriented, refine it into software-oriented technical work.
- If the parent task is about documentation, onboarding, research, design, setup, media, content, or mixed deliverables, keep the refinement inside that domain.
- Do not force every refined task to become source-code work.

Hard requirements:
- The input task is high-level and not directly executable.
- Your output must decompose it into refined tasks.
- Each refined task must be concrete, bounded, and execution-oriented within its own domain.
- Do not generate atomic file-level steps yet.
- Do not generate vague tasks.
- proposed_solution must explain the intended approach.
- implementation_steps must be a list of concrete execution-oriented steps.
- tests_required must define how the refined task should be validated.
- acceptance_criteria must be a single string, not an array.
- Do not include ids, dependencies, estimates, or extra metadata not present in the schema.

Refinement rules:
- Prefer splitting by responsibility when a task contains multiple concerns.
- Preserve the intent of the parent task while reducing ambiguity.
- Refined tasks must be useful inputs for a later Atomic Task Generator.
- If the parent task is documentation or onboarding, refine it into smaller documentation/onboarding deliverables.
- If the parent task is implementation, refine it into concrete subtasks without jumping to atomic work.
- If the parent task includes research, analysis, domain modeling, planning, or validation, refine it into bounded outputs that can guide later execution.
- Do not force documentation or onboarding subtasks if they are not natural parts of the parent task.

Anti-coupling rules:
- Do not assume user-facing project entities should reuse the platform's internal orchestration entities.
- Do not map business entities onto Project, Task, Artifact, or ExecutionRun by default.
- Only reuse platform internals if the parent task explicitly requires extending the platform itself.

Completeness self-check:
Before finalizing the refinement, silently verify whether the parent task has been decomposed into a complete enough set of refined tasks for its purpose.

Examples of areas to consider when relevant:
- domain clarification
- structure or design choices
- implementation path
- validation/testing
- supporting docs or handoff material
- setup or operational guidance

Self-check rules:
- Do not force all of these areas into every refinement.
- Include only what is genuinely needed for the parent task.
- If the parent task is software-oriented, testing and technical clarification are often relevant and should be considered.
- If the parent task belongs to another domain, keep the refinement natural to that domain.
- If a meaningful sub-area is missing, add a refined task for it.
- Do not add filler tasks.

Future-proofing rules:
- Refined tasks should remain compatible with a future multi-executor system.
- Do not assume every refined task ends in code generation.
- A refined task may later decompose into atomic tasks for multiple executor types.
- Keep the task grounded in the real deliverable domain so that later stages can assign the right executor per atomic unit.
"""


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
    strict_schema = to_openai_strict_json_schema(
        TechnicalTaskRefinementOutput.model_json_schema()
    )

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
        json_schema=strict_schema,
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
            json_schema=strict_schema,
        )
        return TechnicalTaskRefinementOutput.model_validate(raw_retry)
