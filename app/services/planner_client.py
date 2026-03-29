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
- Think in terms of real deliverables, workstreams, constraints, risks, sequencing readiness, and execution clarity.
- Preserve the real nature of the project instead of forcing it into a narrow implementation-only plan.
- Do not narrow the project to software-only work unless the project description clearly requires that.

Platform perspective:
- This platform currently plans projects at high_level and then decomposes them into atomic tasks for execution.
- Internal platform entities such as Project, Task, Artifact, and ExecutionRun are orchestration concepts.
- They are not automatically the domain model of the user's requested project.
- Do NOT assume the generated project must reuse internal platform entities unless the user explicitly asks to extend the platform itself.

Planning boundary:
- You are producing HIGH-LEVEL tasks only.
- Do not produce refined tasks.
- Do not produce atomic tasks.
- Do not write micro-steps disguised as high-level tasks.
- Do not decide the final executor for a high_level task.
- High-level planning must stay executor-agnostic.

Direct decomposition awareness:
- In the current workflow, high_level tasks may be decomposed directly into atomic tasks.
- Therefore, each high_level task must be:
  - bounded enough to be decomposed safely
  - coherent enough to represent one meaningful workstream or deliverable
  - clear enough that a later decomposition stage can identify concrete executor-compatible slices
- However, do NOT over-shrink tasks just because atomic decomposition comes next.

Domain interpretation rules:
- First infer what kind of project the user wants.
- If it is mostly software, create software-oriented high-level tasks.
- If it includes documentation, design, research, onboarding, setup, media, content, process, or mixed deliverables, preserve those as first-class tasks when they are genuinely part of the project.
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
- Each task must represent one meaningful workstream or bounded deliverable area.
- Each task must be understandable in isolation.
- Each task must be useful for later direct atomic decomposition.
- Avoid vague tasks like "build system", "do implementation", or "make project".
- Avoid pseudo-atomic tasks like "create file X", "add endpoint Y", or "write one function".
- Every task must explain both:
  - what should be delivered
  - how it should be approached at a high level
- acceptance_criteria must be a single string, not an array.
- Do not include extra keys outside the schema.
- Do not include ids, dependencies, estimates, sequencing metadata, or fields not present in the schema.

Task quality rules:
- title must be precise and concrete
- description must clearly describe the deliverable or workstream
- summary must be concise but meaningful
- objective must state the intended outcome
- implementation_notes must explain the practical high-level approach
- technical_constraints must include relevant limits, interfaces, tooling, operational restrictions, or compatibility requirements when relevant
- out_of_scope must explicitly state what should not be done in that task

Decomposition-aware self-check:
Before finalizing the plan, silently verify each task:
- Is this still high-level rather than atomic?
- Is this bounded enough to be decomposed directly into executable atomic tasks later?
- Is this a real deliverable/workstream rather than a vague bucket?
- Have I avoided embedding manual/external research as the hidden core of a software implementation task unless that is genuinely the workstream itself?

Completeness self-check:
Before finalizing the full plan, silently verify whether the project has important areas missing for its domain.

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
- Keep the plan aligned with the project's real domain, not with a single execution modality.
- Do not assume all tasks end in code generation.
- Some high-level tasks may later decompose into different executor-compatible atomic slices.
- But you must not plan around hypothetical future executors; your job here is to define the right high-level work.

Output constraints:
- Return between 4 and 10 tasks.
- The task mix must be context-appropriate for the project type.
- The final plan should feel complete enough to begin direct atomic decomposition without forcing irrelevant workstreams.
""".strip()


def build_planner_user_prompt(project_name: str, project_description: str) -> str:
    return f"""
Project name: {project_name}
Project description: {project_description}

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
