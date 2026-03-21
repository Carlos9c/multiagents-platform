from pydantic import ValidationError

from app.schemas.planner import PlannerOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


PLANNER_SYSTEM_PROMPT = """
You are a senior technical planning agent.

Your job is to convert a software project description into a detailed, executable implementation plan.

Return ONLY JSON matching the provided schema.

Hard requirements:
- The plan MUST include at least one task with task_type="documentation".
- The plan MUST include at least one task with task_type="onboarding".
- The plan MUST include at least one task with task_type="implementation".
- Documentation and onboarding are mandatory deliverables, not optional recommendations.
- If you omit any of these required task types, your answer is invalid.

Classification rules:
- If a task creates architecture documents, technical documents, API docs, README files, operational docs, setup docs, or usage instructions for the system, classify it as task_type="documentation".
- If a task creates onboarding material, developer setup instructions, first-run guides, quickstart guides, or user/developer usage guidance, classify it as task_type="onboarding".
- Do not classify documentation work as "design".
- Do not classify onboarding or usage-guide work as "implementation".

Planning rules:
- Every task must explain both:
  - what should be done
  - how it should be approached
- Avoid vague tasks like "build backend", "implement system", or "create API".
- Each task must be bounded, specific, testable, and understandable in isolation.
- acceptance_criteria must be a single string, not an array.
- Do not include extra keys outside the schema.
- Do not include task ids, dependencies, estimates, or metadata not present in the schema.
- The generated plan must be coherent with a backend application that already persists projects, tasks, artifacts, and execution runs.

Task quality rules:
- title must be concrete and precise
- description must explain the deliverable clearly
- summary must be concise but informative
- objective must state the intended result
- implementation_notes must explain the practical approach
- technical_constraints must include architectural or operational limits
- out_of_scope must explicitly state what should not be done

Output constraints:
- Return between 4 and 10 tasks.
- Include at least one documentation task focused on technical/project documentation.
- Include at least one onboarding task focused on setup, quickstart, or usage guidance.
- Include at least one implementation task focused on actual backend or service implementation.
"""


def build_planner_user_prompt(project_name: str, project_description: str) -> str:
    return f"""
Project name:
{project_name}

Project description:
{project_description}

Important:
A good plan for this system must include documentation and a usage guide as first-class tasks, not as optional notes.

Mandatory task types:
- at least one documentation task
- at least one onboarding task
- at least one implementation task

Reminder:
- documentation tasks must be explicitly labeled as task_type="documentation"
- onboarding or quickstart tasks must be explicitly labeled as task_type="onboarding"
- do not classify documentation as design
- do not classify onboarding as implementation
""".strip()


def build_planner_retry_prompt(
    project_name: str,
    project_description: str,
    validation_error: str,
) -> str:
    return f"""
Project name:
{project_name}

Project description:
{project_description}

Your previous answer was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Mandatory corrections:
- include at least one task with task_type="documentation"
- include at least one task with task_type="onboarding"
- include at least one task with task_type="implementation"

Important:
- if a task produces architecture docs, technical docs, setup docs, README, operational docs, or API docs, classify it as "documentation"
- if a task produces quickstart, onboarding, first-run guidance, or usage guide material, classify it as "onboarding"
- do not classify documentation as "design"
- do not classify onboarding as "implementation"
""".strip()


def call_planner_model(project_name: str, project_description: str) -> PlannerOutput:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(PlannerOutput.model_json_schema())

    first_user_prompt = build_planner_user_prompt(
        project_name=project_name,
        project_description=project_description,
    )

    raw = provider.generate_structured(
        system_prompt=PLANNER_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="planner_output",
        json_schema=strict_schema,
    )

    try:
        return PlannerOutput.model_validate(raw)
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
            json_schema=strict_schema,
        )

        return PlannerOutput.model_validate(raw_retry)