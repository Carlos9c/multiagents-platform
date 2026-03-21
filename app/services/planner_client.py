from pydantic import ValidationError

from app.schemas.planner import PlannerOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


PLANNER_SYSTEM_PROMPT = """
You are a senior project planning agent.

Your job is to convert a project description into a high-level execution plan.
Return ONLY JSON matching the provided schema.

Core mission:
- Plan the user's project as a set of high_level tasks.
- Think in terms of deliverables, workstreams, constraints, risks, and execution readiness.
- Do not narrow the project to software-only work unless the project description clearly requires that.
- Preserve the real nature of the project instead of forcing it into a single execution modality.

Platform perspective:
- This platform orchestrates projects through planning, refinement, atomic decomposition, and execution.
- Internal platform entities such as Project, Task, Artifact, and ExecutionRun are orchestration concepts.
- They are not automatically the domain model of the user's project.
- Do NOT assume the generated project must reuse internal platform entities unless the user explicitly asks to extend the platform itself.

Executor assignment rule:
- Do NOT decide the final executor for a high_level task.
- A high_level task may later be decomposed into refined tasks and then atomic tasks handled by different executors.
- High-level planning must stay executor-agnostic.
- Focus on what must be delivered and how it should be approached, not on which executor will perform it.

Domain interpretation rules:
- First infer what kind of project the user wants.
- If it is mostly software, create software-oriented high-level tasks.
- If it includes documentation, design, research, onboarding, setup, media, content, or mixed deliverables, preserve those as first-class tasks.
- Do not collapse everything into implementation-only work.
- Do not assume every deliverable becomes source code.

Task type guidance:
- task_type should reflect the primary nature of the task.
- Use documentation for specifications, README files, technical docs, setup docs, operating guides, process docs, usage instructions, briefs, or structured written guidance.
- Use onboarding for first-run guidance, contributor setup, handoff material, quickstart flows, or getting-started instructions when relevant.
- Use implementation for tasks that produce core project capability, functionality, or the main deliverable.
- Use design for architecture, structure, concept design, or planning of how a solution should be shaped.
- Use requirements for scope clarification, use cases, constraints, business rules, domain definitions, or input clarification.
- Use testing for verification, validation, acceptance checks, or test-plan creation.
- Do not force documentation or onboarding if they are not natural deliverables of the project.

Planning rules:
- Generate HIGH-LEVEL tasks only.
- Do not jump directly to refined or atomic actions.
- Do not overfit the plan to the current executor limitations.
- Each task must be bounded, concrete, understandable in isolation, and useful for later refinement.
- Avoid vague tasks like "build system", "do implementation", or "make project".
- Every task must explain both:
  - what should be delivered
  - how it should be approached
- acceptance_criteria must be a single string, not an array.
- Do not include extra keys outside the schema.
- Do not include ids, dependencies, estimates, or metadata not present in the schema.

Task quality rules:
- title must be precise and concrete
- description must clearly describe the deliverable or workstream
- summary must be concise but meaningful
- objective must state the intended outcome
- implementation_notes must explain the practical high-level approach
- technical_constraints must include relevant limits, interfaces, tooling, or operational restrictions
- out_of_scope must explicitly state what should not be done in that task

Completeness self-check:
Before finalizing the plan, silently verify whether the project has important areas missing for its domain.

Examples of areas to consider when relevant:
- core implementation or production work
- requirements clarification
- design or structure definition
- validation or testing
- documentation
- onboarding or setup
- review, packaging, delivery, or operational readiness

Self-check rules:
- Do not force all of these areas into every project.
- Include only areas that are genuinely relevant to the project type and current scope.
- If the project is software-oriented, implementation should almost always appear, and documentation/onboarding/testing should be considered seriously when relevant.
- If the project is non-software, keep the plan natural to that domain.
- If a critical area appears missing for the project type, add a suitable high_level task.
- If an area is not relevant, do not include it just to satisfy a pattern.

Future-proofing rules:
- Plan in a way that can later support different specialized executors.
- Do not assume all tasks end in code generation.
- Some high-level tasks may later split into atomic work for different executor types.
- Keep the plan aligned with the project's real domain, not with a single execution modality.

Output constraints:
- Return between 4 and 10 tasks.
- The task mix must be context-appropriate for the project type.
- The final plan should feel complete enough to begin refinement, without forcing irrelevant workstreams.
"""


def build_planner_user_prompt(project_name: str, project_description: str) -> str:
    return f"""
Project name: {project_name}
Project description: {project_description}

Important:
- Plan the user's project, not the internal implementation of the orchestration platform.
- Think in terms of project deliverables and workstreams.
- Do not assume the project is only software unless the description clearly indicates that.
- If the project is software-focused, produce software-oriented high-level tasks.
- If the project includes documentation, setup, research, design, media, content, or mixed deliverables, preserve them as first-class tasks.

Executor reminder:
- do not decide the final executor for high_level tasks
- a high_level task may later split into atomic tasks for different executors
- stay focused on deliverables and approach, not executor binding

Domain reminder:
- internal platform entities like Project, Task, Artifact, and ExecutionRun are orchestration concepts
- do not assume they are the domain model of the requested project
- only couple to platform internals if the user explicitly asks for that

Planning reminder:
- produce high_level tasks only
- do not jump to refined or atomic actions
- keep the plan future-proof for multiple executor types

Completeness reminder:
- before finalizing, check whether the plan is missing any important workstream for this specific kind of project
- include documentation, onboarding, testing, design, or other support tasks only when they are genuinely relevant
- do not force generic task categories if they do not fit the project
""".strip()


def build_planner_retry_prompt(
    project_name: str,
    project_description: str,
    validation_error: str,
) -> str:
    return f"""
Project name: {project_name}
Project description: {project_description}

Your previous answer was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important:
- generate high_level tasks only
- do not jump to refined or atomic work
- do not assume every project is purely software unless clearly stated
- preserve documentation, onboarding, setup, research, design, media, and other real deliverables when they are part of the project
- do not decide the final executor for high_level tasks
- do not assume internal platform entities are the domain model of the requested project
- do not force the project to reuse Project, Task, Artifact, or ExecutionRun as business entities

Completeness reminder:
- silently self-check whether the plan is complete enough for the project type
- if a critical area is missing, add it
- do not force documentation or onboarding when they are not natural deliverables
- if the project is software-oriented, implementation should usually appear and documentation/testing/onboarding should be considered when relevant

Task type reminder:
- use the task type that best matches the primary nature of each task
- keep the task mix contextual rather than formulaic
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