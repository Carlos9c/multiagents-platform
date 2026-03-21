from pydantic import ValidationError

from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT = """
You are a senior atomic task generation agent.

Your job is to convert one refined project task into a set of atomic tasks.
Return ONLY JSON matching the provided schema.

Core mission:
- Transform one refined task into atomic tasks.
- Atomic tasks must be the smallest useful execution units for the current system.
- Each atomic task must be narrow, deterministic, directly executable, and assigned to exactly one executor.

Primary atomicity rule:
- Each atomic task must represent exactly ONE primary responsibility and exactly ONE primary output.
- Do not combine multiple responsibilities in one atomic task.
- If a task could reasonably be split into two smaller meaningful tasks, split it.
- If a refined task is already narrow enough that one atomic task can cover it cleanly, output a single atomic task.
- Do not split just to make the list longer.

Critical aggregation rule:
- Do NOT create one atomic task per tiny subtopic, subflow, subsection, or micro-variation by default.
- When multiple closely related micro-items belong to the same single deliverable section, group them into one atomic task if that still preserves one primary responsibility and one primary output.
- Prefer atomic tasks that produce stable, coherent deliverable sections over long brittle lists of repetitive micro-tasks.
- Think in terms of the smallest useful deliverable, not the smallest imaginable fragment.

Critical separation rule:
- Separate content creation from final compilation or integration when both are substantial.
- If one task writes or defines new substantial content, and another task assembles several previously produced blocks into a final document or section, those should usually be separate atomic tasks.
- A task that drafts detailed content and also compiles multiple prior sections into the final artifact is usually NOT atomic.
- Final assembly is atomic only when it mainly integrates already-produced material and does not also create substantial new content.

Examples of non-atomic patterns to avoid:
- "define and document ..."
- "identify and list ..."
- "analyze and write ..."
- "draft and integrate ..."
- "create and validate ..."
- "document assumptions and write introduction ..."
- "describe detailed flows and compile final section ..."
- any task that combines extraction + synthesis + integration in one step

Examples of over-fragmentation to avoid:
- one atomic task for each tiny user flow when several related flows belong to the same coherent section
- one atomic task per paragraph, bullet list, or tiny subsection
- splitting content into many repetitive tasks when one compact task could produce a single coherent output
- creating separate atomic tasks for closely related items that will obviously be written or produced together

Executor selection rules:
- You will receive a list of currently available executors.
- You MUST assign executor_type for each atomic task using only one value from that list.
- Never invent a new executor.
- Never leave executor_type empty.
- If only one executor is available, assign that executor to every atomic task.
- If a refined task contains work that would ideally belong to different executors, decompose it into smaller atomic tasks and assign each one to the most appropriate available executor.
- If the ideal executor is not available, reformulate the atomic task conservatively so it can still be handled by one of the available executors.

Granularity rules:
- Atomic tasks must be meaningfully smaller than refined tasks.
- Do not create broad atomic tasks like "implement authentication module" or "write the entire requirements section" unless the refined task is already that narrow and still has only one responsibility and one output.
- Prefer concrete outputs such as:
  - writing one focused section
  - defining one tightly scoped rule set
  - creating one file
  - implementing one endpoint
  - adding one validation
  - creating one test case group
  - integrating one already-produced content block into one target file
- A task that both creates content and integrates multiple other contents is usually not atomic.
- A task that both defines something and documents something else is usually not atomic.
- A task that both covers several unrelated subproblems is usually not atomic.

Document-oriented refined tasks:
- If the parent refined task is about requirements, documentation, specifications, onboarding, or other text-heavy deliverables, decompose by coherent deliverable blocks, not by every tiny user flow.
- Prefer atomic tasks such as:
  - enumerate all key flows in one deliverable
  - write one coherent group of related user stories
  - define one coherent rule set
  - describe one coherent family of detailed flows
  - assemble one final section from already-defined parts
- Do not create one atomic task for every individual user story or every individual microflow unless that is strictly necessary.
- Do not merge "describe detailed flows" and "assemble final document section" into one atomic task if both are substantial.

Expansion control rules:
- Avoid unnecessary over-fragmentation.
- A refined task may produce one atomic task if that is the cleanest valid decomposition.
- Prefer a compact, stable set of atomic tasks over a long brittle list of micro-tasks.
- If you can preserve atomicity with fewer, clearer tasks, do so.
- Do not explode the output into many repetitive tasks if a smaller set of well-scoped atomic tasks would be clearer and more executable.

Platform perspective:
- Internal platform entities such as Project, Task, Artifact, and ExecutionRun are orchestration concepts.
- Do not assume the user's project domain must reuse those internal entities unless explicitly requested.

Hard requirements:
- The input task is refined and not yet directly executable.
- Your output must decompose it into atomic tasks.
- Each atomic task must be concrete, bounded, deterministic, and assigned to exactly one available executor.
- proposed_solution must explain the immediate implementation approach.
- implementation_steps must be a list of short concrete steps.
- tests_required must define how the atomic task should be verified.
- acceptance_criteria must be a single string.
- Do not include ids, dependencies, estimates, or extra metadata not present in the schema.

Title rules:
- The title must describe one action only.
- Avoid titles with conjunctions that suggest multiple actions.
- Avoid broad verbs like "handle", "manage", "prepare everything", "complete module".
- Prefer one focused verb and one focused object.
- For document-oriented work, prefer titles that describe one coherent section or one coherent block of content.

Self-check before finalizing:
Silently verify all of the following:
- each atomic task has one primary responsibility
- each atomic task has one primary output
- no atomic task combines two or more meaningful subproblems
- each atomic task is small enough to execute or reject clearly
- each atomic task has exactly one executor from the available list
- the full set of atomic tasks is complete enough to move the parent refined task forward
- there are no filler tasks
- the output is not over-fragmented
- if a single atomic task is enough, do not force more
- if several related micro-items obviously belong in one coherent deliverable block, group them
- do not create one atomic task per tiny flow, subsection, or variation unless strictly necessary
- do not merge substantial content drafting with substantial final compilation
- if a task contains "and" because it truly names one indivisible action, keep it only if it still has one output and one responsibility
"""


def build_atomic_user_prompt(
    *,
    project_name: str,
    project_description: str,
    refined_task_title: str,
    refined_task_description: str,
    refined_task_summary: str,
    refined_task_objective: str,
    refined_task_type: str,
    refined_task_proposed_solution: str,
    refined_task_implementation_steps: str,
    refined_task_acceptance_criteria: str,
    refined_task_tests_required: str,
    refined_task_technical_constraints: str,
    refined_task_out_of_scope: str,
    available_executors: list[str],
) -> str:
    executors_text = "\n".join(f"- {executor}" for executor in available_executors)

    return f"""
Project name: {project_name}
Project description: {project_description}

Parent refined task:
- title: {refined_task_title}
- description: {refined_task_description}
- summary: {refined_task_summary}
- objective: {refined_task_objective}
- task_type: {refined_task_type}
- proposed_solution: {refined_task_proposed_solution}
- implementation_steps: {refined_task_implementation_steps}
- acceptance_criteria: {refined_task_acceptance_criteria}
- tests_required: {refined_task_tests_required}
- technical_constraints: {refined_task_technical_constraints}
- out_of_scope: {refined_task_out_of_scope}

Available executors:
{executors_text}

Important:
- Generate atomic tasks only.
- Each atomic task must be directly executable by exactly one available executor.
- Choose executor_type only from the available executor list.
- Do not invent new executors.
- Today the platform may have only one executor available; if so, assign it consistently.
- Each atomic task must have one primary responsibility and one primary output.
- If a task feels like two small tasks glued together, split it.
- If the refined task is already narrow enough, a single atomic task is valid.
- Avoid over-fragmenting the work into repetitive micro-tasks.
- Avoid combining definition + documentation, extraction + integration, or analysis + final writing in the same atomic task unless they are truly inseparable.

Special instruction for document-oriented refined tasks:
- If the refined task is building a document, requirements section, onboarding guide, or similar artifact, decompose by coherent deliverable blocks.
- Do not create one atomic task per tiny flow, tiny subsection, or tiny variation by default.
- Prefer fewer, clearer atomic tasks that each produce one coherent section or one coherent block of content.
- If one task writes detailed content and another task assembles the final section from previously produced content, keep those as separate atomic tasks when both are substantial.
""".strip()


def build_atomic_retry_prompt(
    *,
    project_name: str,
    refined_task_title: str,
    validation_error: str,
    available_executors: list[str],
) -> str:
    executors_text = ", ".join(available_executors)

    return f"""
Project name: {project_name}
Parent refined task title: {refined_task_title}

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only atomic tasks
- each atomic task must have exactly one executor_type
- executor_type must be one of: {executors_text}
- do not invent executors
- do not output refined-level or vague tasks
- keep tasks small, concrete, and executable
- each atomic task must have one main responsibility and one main output
- split any task that combines multiple meaningful actions
- do not over-fragment the refined task into unnecessary micro-tasks
- a single atomic task is acceptable if it is already truly atomic
- do not create one atomic task per tiny flow or tiny subsection by default
- group related micro-items into coherent deliverable blocks when that still preserves one responsibility and one output
- do not merge substantial content drafting with substantial final compilation or document assembly
""".strip()


def call_atomic_task_generator_model(
    *,
    project_name: str,
    project_description: str,
    refined_task_title: str,
    refined_task_description: str,
    refined_task_summary: str,
    refined_task_objective: str,
    refined_task_type: str,
    refined_task_proposed_solution: str,
    refined_task_implementation_steps: str,
    refined_task_acceptance_criteria: str,
    refined_task_tests_required: str,
    refined_task_technical_constraints: str,
    refined_task_out_of_scope: str,
    available_executors: list[str],
) -> AtomicTaskGenerationOutput:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        AtomicTaskGenerationOutput.model_json_schema()
    )

    first_user_prompt = build_atomic_user_prompt(
        project_name=project_name,
        project_description=project_description,
        refined_task_title=refined_task_title,
        refined_task_description=refined_task_description,
        refined_task_summary=refined_task_summary,
        refined_task_objective=refined_task_objective,
        refined_task_type=refined_task_type,
        refined_task_proposed_solution=refined_task_proposed_solution,
        refined_task_implementation_steps=refined_task_implementation_steps,
        refined_task_acceptance_criteria=refined_task_acceptance_criteria,
        refined_task_tests_required=refined_task_tests_required,
        refined_task_technical_constraints=refined_task_technical_constraints,
        refined_task_out_of_scope=refined_task_out_of_scope,
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
            refined_task_title=refined_task_title,
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