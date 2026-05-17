from pydantic import ValidationError

from app.execution_engine.capabilities import render_executor_capabilities_for_prompt
from app.models.task import VALID_EXECUTOR_TYPES
from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput
from app.services.llm.factory import get_llm_provider

ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT = """
You are a senior atomic task generation agent.

Your job is to convert one parent project task into a set of atomic tasks that the CURRENT SYSTEM can actually execute.
Return ONLY JSON matching the provided schema.

Primary principle:
- Atomicity is not decided only by semantic neatness.
- Atomicity must be decided by REAL execution capability.
- A task is atomic only if exactly one currently available executor can complete it end to end with the capabilities it actually has.

Current system reality:
- The active execution target is execution_engine.
- Do not reason about hypothetical future executors.
- Do not optimize for a future multi-executor platform.
- Optimize for the executor list that is explicitly provided in the prompt.
- You must reason from the actual execution-engine subagents and tools listed in the prompt.

Task design rules:
- Prefer tasks that end in a concrete, inspectable deliverable (artifact, document, configuration, output, or workspace change).
- Prefer tasks whose result can be validated from observable evidence after execution.
- Keep manual investigation, external research, human-only validation, and stakeholder judgment out of the core deliverable.
- Bootstrap from an empty repository is allowed only when the task objective clearly implies a minimal initial structure.

Hard executor-oriented rule:
- If the parent task mixes executable work with non-executable research/manual work, do NOT keep them mixed in one atomic task.
- Extract only the executable slice.
- Reformulate the task around a concrete, verifiable deliverable whenever possible.

Atomicity rules:
- Each atomic task must have one primary deliverable.
- Each atomic task must have one clear validation boundary.
- Each atomic task must be directly executable by exactly one available executor.
- Avoid overlap, duplication, and artificial fragmentation.
- A task must address one focused functional concern. It must NOT span multiple independent functional areas, multiple architectural layers, or the "core" of an entire system in one shot.

Scope calibration rule:
- The right granularity is a focused functional unit — one component, one feature slice, one data layer element, one screen, one service, one document section, one configuration block.
- A task may naturally include tightly-coupled supporting artifacts (e.g., a data model together with its persistence mapping, a feature together with its directly-tied tests) as long as they form a single indivisible deliverable.
- A task is too broad when: it spans multiple independent feature areas, it would produce a significant fraction of the project in one step, or there are two or more natural checkpoints within it where you could say "this part is done independently."
- A task is too narrow when: splitting it would create invalid intermediate states that block further progress, or the pieces have no independent validation boundary.

When to split:
- split when there are clearly separate deliverables that can be validated independently
- split when there are clearly separate validation boundaries
- split when executable work is mixed with manual/research work
- split when two feature slices or functional areas can be implemented independently
- split when one part is executable by the available executor and another part is not
- split when the scope covers an entire module, layer, subsystem, or "core" of the project
- split when there are multiple natural completion checkpoints within the task

When NOT to split:
- one focused functional unit with a single validation boundary
- tightly-coupled artifacts that have no independent validation boundary on their own
- one coherent document, specification, or configuration deliverable
- a feature slice together with its directly-tied verification if both are modest in scope

Forbidden task patterns for the execution engine:
- “investigate the real runtime behavior and document findings”
- “run the system manually to understand how it works” as the main task
- “collect and validate real operational information” as the main task
- “analyze options and recommend approach” without producing a concrete repo artifact
- “manually verify” as the central acceptance path

Required output quality:
- proposed_solution must explain the immediate executor-compatible approach
- implementation_steps must be concrete and repository-oriented
- tests_required must describe checks aligned with the deliverable
- acceptance_criteria must be a single string
- do not assign or emit a final executor in the output
- atomic generation must judge executor compatibility, but executor routing is resolved later by orchestration
- never invent new executors or future capabilities
- do not include ids, dependencies, estimates, or metadata outside the schema

Task type assignment rules:
Assign the task_type that best reflects the primary nature of each atomic task.
Valid values and when to use them:
- implementation: produces core functionality, services, modules, API endpoints, or main code deliverables
- testing: primary output is test files, verification scripts, or acceptance checks — use this, never "test"
- documentation: produces written deliverables — README, specs, technical docs, setup guides, usage instructions
- design: produces architecture definitions, interface contracts, data models, or design decisions as repo files
- requirements: clarifies scope, defines use cases, or produces domain constraint documents
- planning: decomposes, sequences, or roadmaps work into a concrete repo artifact
- review: audits or evaluates existing deliverables without new primary output (rare for atomic tasks)
- onboarding: produces contributor setup guides, quickstart docs, or handoff material
- configuration: produces environment setup, CI/CD pipelines, tooling config, or infrastructure-as-code
- refactor: restructures existing code without changing external behavior

Self-check before finalizing each atomic task:
- Can the current execution target really complete this task with its actual capabilities?
- Is the main deliverable concrete and inspectable after execution?
- Would post-execution validation be able to confirm the result from observable evidence?
- Is this task free from hidden manual/external work?
- Does this task address one focused functional concern, or does it span multiple independent areas?
- Are there two or more natural completion checkpoints within this task? If so, split it.
- Does the task cover an entire module, layer, subsystem, or "core" of the project? If so, split it.

Language rule:
- Detect the language of the parent task description.
- Generate ALL output fields (title, proposed_solution, implementation_steps, tests_required, acceptance_criteria, technical_constraints) in that same language.
- If the parent task is in Spanish, respond entirely in Spanish.
- If in English, respond in English. Never mix languages within a single response.
""".strip()


def _validate_available_executors(available_executors: list[str]) -> list[str]:
    invalid_executors = [
        executor for executor in available_executors if executor not in VALID_EXECUTOR_TYPES
    ]
    if invalid_executors:
        raise ValueError(
            "Invalid available_executors values: "
            f"{invalid_executors}. Allowed values: {sorted(VALID_EXECUTOR_TYPES)}"
        )
    return list(available_executors)


def _build_executor_capabilities_text(available_executors: list[str]) -> str:
    executors = _validate_available_executors(available_executors)
    blocks: list[str] = []
    for executor in executors:
        blocks.append(render_executor_capabilities_for_prompt(executor))
    return "\n\n".join(blocks)


def build_atomic_user_prompt(
    *,
    project_name: str,
    project_description: str,
    parent_task_title: str,
    parent_task_description: str,
    parent_task_summary: str,
    parent_task_objective: str,
    parent_task_type: str,
    parent_task_planning_level: str,
    parent_task_proposed_solution: str,
    parent_task_implementation_steps: str,
    parent_task_acceptance_criteria: str,
    parent_task_tests_required: str,
    parent_task_technical_constraints: str,
    parent_task_out_of_scope: str,
    available_executors: list[str],
) -> str:
    executors = _validate_available_executors(available_executors)
    executors_text = "\n".join(f"- {executor}" for executor in executors)
    capability_text = _build_executor_capabilities_text(executors)

    return f"""
Project name: {project_name}
Project description: {project_description}

Parent task to atomize:
- title: {parent_task_title}
- description: {parent_task_description}
- summary: {parent_task_summary}
- objective: {parent_task_objective}
- task_type: {parent_task_type}
- planning_level: {parent_task_planning_level}
- proposed_solution: {parent_task_proposed_solution}
- implementation_steps: {parent_task_implementation_steps}
- acceptance_criteria: {parent_task_acceptance_criteria}
- tests_required: {parent_task_tests_required}
- technical_constraints: {parent_task_technical_constraints}
- out_of_scope: {parent_task_out_of_scope}

Available executors:
{executors_text}

Executor capability catalogs:
{capability_text}

Mandatory instructions:
- Generate atomic tasks only.
- Each atomic task must be executable by exactly one available executor.
- Do NOT output a final executor assignment.
- Use the available executor list only to decide whether the task is truly executable by the current system.
- Never invent executors.
- Judge atomicity using BOTH:
  1) one primary deliverable and one validation boundary
  2) real executor capability compatibility
- Prefer the smallest number of tasks that remain truly executable.
- Avoid overlap.
- For the execution engine, rely on the listed subagents and tools instead of generic assumptions.

For the execution engine specifically:
- Prefer tasks that end in a concrete repository deliverable.
- Good deliverables include source files, test files, config files, or repository documentation files.
- Bad deliverables include open-ended investigation, manual verification, and external information gathering.
- Do not assign the execution engine a task whose core output depends on observing runtime behavior manually.
- Do not assign the execution engine a task whose core output is “analyze and document findings” unless the analysis is directly tied to a concrete repository artifact that can be produced from repo context.

Split when:
- there are clearly separate deliverables that can be validated independently
- there are clearly separate validation boundaries
- executable work is mixed with manual/research work
- two feature slices or functional areas can be completed independently
- the scope covers an entire module, layer, subsystem, or "core" of the project
- there are multiple natural completion checkpoints within the task

Do NOT split when:
- the deliverable is one focused functional unit with a single validation boundary
- tightly-coupled artifacts have no independent validation boundary on their own
- a feature slice together with its directly-tied verification is modest in combined scope

Important:
- Rewrite the task around what the executor can actually finish.
- If a portion of the parent task is not executable by the available executors, do not make that non-executable portion the core of an atomic task.
- Prefer tasks that are individually completable and verifiable over tasks that are oversized and produce partial results.
- A task may be broader than a single file or class as long as it addresses one focused functional concern with one validation boundary.
""".strip()


def build_atomic_retry_prompt(
    *,
    validation_error: str,
    project_name: str,
    parent_task_title: str,
    available_executors: list[str],
) -> str:
    executors = _validate_available_executors(available_executors)
    executors_text = ", ".join(executors)
    capability_text = _build_executor_capabilities_text(executors)

    return f"""
Project name: {project_name}
Parent task title: {parent_task_title}

Your previous output was invalid.

Validation error:
{validation_error}

Available executors: {executors_text}

Executor capability catalogs:
{capability_text}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only atomic tasks
- do not include executor_type in the output
- use the available executor list only as a capability constraint for atomicity
- each atomic task must have one primary deliverable and one validation boundary
- each atomic task must be compatible with the REAL capabilities of the assigned executor
- do not invent future executors or hypothetical capabilities
- for the execution engine, require a concrete repository/file deliverable
- use the listed execution-engine subagents and tools as the source of truth for what is realistically doable
- do not make manual investigation, external research, or manual validation the core of an execution engine task
- avoid overlap
- do not over-fragment
- split only when there are clearly separate deliverables, clearly separate validation boundaries, or executable and non-executable work are mixed
""".strip()


def call_atomic_task_generator_model(
    *,
    project_name: str,
    project_description: str,
    parent_task_title: str,
    parent_task_description: str,
    parent_task_summary: str,
    parent_task_objective: str,
    parent_task_type: str,
    parent_task_planning_level: str,
    parent_task_proposed_solution: str,
    parent_task_implementation_steps: str,
    parent_task_acceptance_criteria: str,
    parent_task_tests_required: str,
    parent_task_technical_constraints: str,
    parent_task_out_of_scope: str,
    available_executors: list[str],
) -> AtomicTaskGenerationOutput:
    provider = get_llm_provider()
    first_user_prompt = build_atomic_user_prompt(
        project_name=project_name,
        project_description=project_description,
        parent_task_title=parent_task_title,
        parent_task_description=parent_task_description,
        parent_task_summary=parent_task_summary,
        parent_task_objective=parent_task_objective,
        parent_task_type=parent_task_type,
        parent_task_planning_level=parent_task_planning_level,
        parent_task_proposed_solution=parent_task_proposed_solution,
        parent_task_implementation_steps=parent_task_implementation_steps,
        parent_task_acceptance_criteria=parent_task_acceptance_criteria,
        parent_task_tests_required=parent_task_tests_required,
        parent_task_technical_constraints=parent_task_technical_constraints,
        parent_task_out_of_scope=parent_task_out_of_scope,
        available_executors=available_executors,
    )

    raw = provider.generate_structured(
        system_prompt=ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="atomic_task_generation_output",
        json_schema=AtomicTaskGenerationOutput.model_json_schema(),
    )

    try:
        return AtomicTaskGenerationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_atomic_retry_prompt(
            validation_error=str(exc),
            project_name=project_name,
            parent_task_title=parent_task_title,
            available_executors=available_executors,
        )

        raw_retry = provider.generate_structured(
            system_prompt=ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="atomic_task_generation_output",
            json_schema=AtomicTaskGenerationOutput.model_json_schema(),
        )

        return AtomicTaskGenerationOutput.model_validate(raw_retry)
