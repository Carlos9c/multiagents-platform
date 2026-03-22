from pydantic import ValidationError

from app.schemas.code_execution import CodeExecutorInput, CodeWorkingSet, CodeFileEditPlan
from app.schemas.code_generation import (
    CodeGenerationFilesResponse,
    CodeGenerationPlanResponse,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


CODE_EXECUTOR_PLAN_SYSTEM_PROMPT = """
You are a senior code execution planning agent.

Your job is to decide whether a code task should proceed and, if so, produce a minimal and safe file edit plan.
Return ONLY JSON matching the provided schema.

Core rules:
- You are planning work for one already-atomic code task.
- You must either:
  - proceed with a concrete file edit plan
  - reject the execution if the task is too ambiguous, too broad, or unsafe to execute as-is
- Do not validate the code. Validation happens later.
- Do not produce prose outside the schema.

Planning rules:
- Keep the change surface as small as reasonably possible.
- Prefer modifying existing inferred target files when appropriate.
- Create new files only when justified.
- Do not expand scope beyond the task objective.
- Do not invent unrelated files or architectural workstreams.
- Use project-relative paths only.
- If the task cannot be executed safely because the context is insufficient or the scope is too broad, reject it.

Rejection guidance:
Reject if any of these apply:
- the task intent is too ambiguous to infer a safe file surface
- the likely file surface is too large for one atomic task
- the task appears under-specified for code execution
- the task appears to require substantial work outside the provided scope

Self-check before finalizing:
- if proceeding, the planned changes must be minimal, coherent, and directly tied to the task objective
- if rejecting, explain the reason clearly and identify what is missing or unsafe
"""


CODE_EXECUTOR_FILES_SYSTEM_PROMPT = """
You are a senior code generation agent.

Your job is to generate final file contents for an already-approved code edit plan.
Return ONLY JSON matching the provided schema.

Core rules:
- You will receive one atomic code task, a resolved execution context, a working set, and an approved edit plan.
- Generate only the files explicitly present in the approved edit plan.
- Use project-relative paths only.
- For action=create, return the complete file contents.
- For action=modify, return the full updated file contents, not partial patches.
- Do not invent extra files.
- Do not validate. Validation happens later.
- Keep changes narrowly aligned to the task objective and acceptance criteria.
- Preserve coherence with the provided file context.
- If a referenced file already has content, produce the updated full content carefully.
- If information is missing, make the most conservative implementation choice consistent with the task.
"""


def _build_working_set_text(working_set: CodeWorkingSet) -> str:
    parts: list[str] = []
    parts.append(f"repo_root: {working_set.repo_root}")
    parts.append(f"target_files: {working_set.target_files}")
    parts.append(f"related_files: {working_set.related_files}")
    parts.append(f"reference_files: {working_set.reference_files}")
    parts.append("repo_guidance:")
    parts.extend([f"- {item}" for item in working_set.repo_guidance])

    parts.append("files:")
    for file_ctx in working_set.files:
        parts.append(f"- path: {file_ctx.path}")
        parts.append(f"  role: {file_ctx.role}")
        parts.append(f"  summary: {file_ctx.summary}")
        if file_ctx.symbols:
            parts.append(f"  symbols: {file_ctx.symbols}")
        if file_ctx.relevant_snippets:
            parts.append("  relevant_snippets:")
            parts.extend([f"    - {snippet}" for snippet in file_ctx.relevant_snippets])
        if file_ctx.content:
            parts.append("  content:")
            parts.append(file_ctx.content)

    return "\n".join(parts)


def _build_plan_user_prompt(
    context: CodeExecutorInput,
    working_set: CodeWorkingSet,
) -> str:
    return f"""
Task:
- task_id: {context.task_id}
- project_id: {context.project_id}
- title: {context.title}
- description: {context.description}
- objective: {context.objective}
- acceptance_criteria: {context.acceptance_criteria}
- technical_constraints: {context.technical_constraints}
- out_of_scope: {context.out_of_scope}
- execution_goal: {context.execution_goal}

Resolved context:
- relevant_decisions: {context.relevant_decisions}
- candidate_modules: {context.candidate_modules}
- candidate_files: {context.candidate_files}
- relevant_symbols: {context.relevant_symbols}
- unresolved_questions: {context.unresolved_questions}

Working set:
{_build_working_set_text(working_set)}

Important:
- Either return decision='proceed' with a minimal file plan, or decision='reject'
- If proceeding, planned_changes must be concrete and minimal
- If rejecting, explain what is missing or unsafe
""".strip()


def _build_files_user_prompt(
    context: CodeExecutorInput,
    working_set: CodeWorkingSet,
    edit_plan: CodeFileEditPlan,
) -> str:
    plan_lines = [
        f"- path: {item.path} | action: {item.action} | purpose: {item.purpose} | rationale: {item.rationale}"
        for item in edit_plan.planned_changes
    ]

    return f"""
Task:
- task_id: {context.task_id}
- project_id: {context.project_id}
- title: {context.title}
- description: {context.description}
- objective: {context.objective}
- acceptance_criteria: {context.acceptance_criteria}
- technical_constraints: {context.technical_constraints}
- out_of_scope: {context.out_of_scope}
- execution_goal: {context.execution_goal}

Working set:
{_build_working_set_text(working_set)}

Approved edit plan:
summary: {edit_plan.summary}
planned_changes:
{chr(10).join(plan_lines)}
assumptions: {edit_plan.assumptions}
local_risks: {edit_plan.local_risks}
notes: {edit_plan.notes}

Important:
- Generate exactly the files from the approved edit plan
- Return full file contents
- Do not invent extra files
- Keep the implementation conservative and scoped
""".strip()


class CodeExecutorClientError(Exception):
    """Base exception for code executor LLM client."""


def plan_file_edits(
    context: CodeExecutorInput,
    working_set: CodeWorkingSet,
) -> CodeGenerationPlanResponse:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        CodeGenerationPlanResponse.model_json_schema()
    )

    user_prompt = _build_plan_user_prompt(
        context=context,
        working_set=working_set,
    )

    raw = provider.generate_structured(
        system_prompt=CODE_EXECUTOR_PLAN_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="code_generation_plan_response",
        json_schema=strict_schema,
    )

    try:
        return CodeGenerationPlanResponse.model_validate(raw)
    except ValidationError as exc:
        raise CodeExecutorClientError(
            f"Invalid structured response from code edit planning model: {str(exc)}"
        ) from exc


def generate_file_contents(
    context: CodeExecutorInput,
    working_set: CodeWorkingSet,
    edit_plan: CodeFileEditPlan,
) -> CodeGenerationFilesResponse:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        CodeGenerationFilesResponse.model_json_schema()
    )

    user_prompt = _build_files_user_prompt(
        context=context,
        working_set=working_set,
        edit_plan=edit_plan,
    )

    raw = provider.generate_structured(
        system_prompt=CODE_EXECUTOR_FILES_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="code_generation_files_response",
        json_schema=strict_schema,
    )

    try:
        return CodeGenerationFilesResponse.model_validate(raw)
    except ValidationError as exc:
        raise CodeExecutorClientError(
            f"Invalid structured response from code file generation model: {str(exc)}"
        ) from exc