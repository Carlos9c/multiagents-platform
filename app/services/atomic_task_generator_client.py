from pydantic import ValidationError

from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT = """
You are a senior atomic task generation agent.

Your job is to convert one parent project task into a set of atomic tasks that the CURRENT SYSTEM can actually execute.
Return ONLY JSON matching the provided schema.

Primary principle:
- Atomicity is not decided only by semantic neatness.
- Atomicity must be decided by REAL executor capability.
- A task is atomic only if exactly one currently available executor can complete it end to end with the capabilities it actually has.

Current system reality:
- The active executor is code_executor.
- Do not reason about hypothetical future executors.
- Do not optimize for a future multi-executor platform.
- Optimize for the executor list that is explicitly provided in the prompt.

What code_executor CAN do:
- read repository context already available inside the project workspace
- inspect existing code files in the repository
- decide which files are relevant
- plan file edits
- create or modify project files
- update tests or test-related files
- produce repository-based deliverables that can be validated from file changes and workspace evidence
- write documentation files only when the documentation itself is a concrete repository deliverable

What code_executor CANNOT be expected to do as the core of an atomic task:
- perform manual human investigation
- gather external real-world information
- validate behavior through human observation outside its execution contract
- perform business/product decisions that require stakeholder judgment
- do open-ended research as the main deliverable
- execute tasks whose main output is “analyze”, “investigate”, “assess”, or “validate manually” without a concrete repository artifact
- execute tasks whose only meaningful result would come from running manual checks outside the repo
- produce a task that depends primarily on unavailable tools, humans, or future executors

Hard executor-oriented rule:
- If the parent task mixes executable code work with non-executable research/manual work, do NOT keep them mixed in one atomic task.
- Extract only the repository-executable slice.
- Reformulate the task around a concrete repo/file deliverable whenever possible.

Atomicity rules:
- Each atomic task must have one primary deliverable.
- Each atomic task must have one clear validation boundary.
- Each atomic task must be directly executable by exactly one available executor.
- Prefer compact, coherent tasks, but never at the cost of executor mismatch.
- Avoid overlap, duplication, and artificial fragmentation.

When to split:
- split when there are clearly separate deliverables
- split when there are clearly separate validation boundaries
- split when implementation and documentation are both substantial and separable
- split when two feature slices can be completed independently
- split when one part is executable by code_executor and another part is not

When NOT to split:
- one coherent implementation slice with one repository-level deliverable
- one coherent repository document deliverable
- one coherent API/code contract change with a single validation boundary
- one small implementation plus its directly related tests if they belong to the same deliverable

Forbidden task patterns for code_executor:
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
- use only executor_type values from the provided executor list
- never invent new executors
- do not include ids, dependencies, estimates, or metadata outside the schema

Self-check before finalizing each atomic task:
- Can the assigned executor really complete this task with its actual capabilities?
- Is the main deliverable a concrete repository or file outcome?
- Would post-execution validation be able to inspect repo/workspace evidence?
- Is this task free from hidden manual/external work?
- Is splitting truly necessary, or am I over-fragmenting?
""".strip()


def _build_executor_capabilities_text(available_executors: list[str]) -> str:
    sections: list[str] = []

    for executor in available_executors:
        if executor == "code_executor":
            sections.append(
                "\n".join(
                    [
                        "- code_executor",
                        "  - can inspect repository files and existing code structure",
                        "  - can plan and apply file edits inside the project workspace",
                        "  - can create/modify source code, tests, configs, and repository documentation files",
                        "  - should receive tasks with concrete repo/file deliverables",
                        "  - should NOT receive tasks whose core deliverable is manual investigation, external research, or human-only validation",
                    ]
                )
            )
        else:
            sections.append(
                "\n".join(
                    [
                        f"- {executor}",
                        "  - no explicit capability profile was provided",
                        "  - use it only if the prompt explicitly includes it in the available executor list",
                    ]
                )
            )

    return "\n".join(sections)


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
    executors_text = "\n".join(f"- {executor}" for executor in available_executors)
    capability_text = _build_executor_capabilities_text(available_executors)

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

Executor capability profiles:
{capability_text}

Mandatory instructions:
- Generate atomic tasks only.
- Each atomic task must be executable by exactly one available executor.
- Choose executor_type only from the available executor list.
- Never invent executors.
- Judge atomicity using BOTH:
  1) one primary deliverable and one validation boundary
  2) real executor capability compatibility
- Prefer the smallest number of tasks that remain truly executable.
- Avoid overlap.

For code_executor specifically:
- Prefer tasks that end in a concrete repository deliverable.
- Good deliverables include source files, test files, config files, or repository documentation files.
- Bad deliverables include open-ended investigation, manual verification, and external information gathering.
- Do not assign code_executor a task whose core output depends on observing runtime behavior manually.
- Do not assign code_executor a task whose core output is “analyze and document findings” unless the analysis is directly tied to a concrete repository artifact that can be produced from repo context.

Split when:
- there are clearly separate repository deliverables
- there are clearly separate validation boundaries
- executable code work is mixed with manual/research work
- two feature slices can be implemented independently

Do NOT split when:
- one coherent repository deliverable is enough
- implementation and directly related tests are part of one validation boundary
- the work is already a compact executor-compatible slice

Important:
- Rewrite the task around what the executor can actually finish.
- If a portion of the parent task is not executable by the available executors, do not make that non-executable portion the core of an atomic task.
- Output one task if one task is enough.
""".strip()


def build_atomic_retry_prompt(
    *,
    project_name: str,
    parent_task_title: str,
    validation_error: str,
    available_executors: list[str],
) -> str:
    executors_text = ", ".join(available_executors)
    capability_text = _build_executor_capabilities_text(available_executors)

    return f"""
Project name: {project_name}
Parent task title: {parent_task_title}

Your previous output was invalid.

Validation error:
{validation_error}

Available executors: {executors_text}

Executor capability profiles:
{capability_text}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only atomic tasks
- each atomic task must have exactly one executor_type
- executor_type must be one of: {executors_text}
- each atomic task must have one primary deliverable and one validation boundary
- each atomic task must be compatible with the REAL capabilities of the assigned executor
- do not invent future executors or hypothetical capabilities
- for code_executor, require a concrete repository/file deliverable
- do not make manual investigation, external research, or manual validation the core of a code_executor task
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
    strict_schema = to_openai_strict_json_schema(
        AtomicTaskGenerationOutput.model_json_schema()
    )

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
        json_schema=strict_schema,
    )

    try:
        return AtomicTaskGenerationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_atomic_retry_prompt(
            project_name=project_name,
            parent_task_title=parent_task_title,
            validation_error=str(exc),
            available_executors=available_executors,
        )

        raw_retry = provider.generate_structured(
            system_prompt=ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="atomic_task_generation_output",
            json_schema=strict_schema,
        )

        return AtomicTaskGenerationOutput.model_validate(raw_retry)