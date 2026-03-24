from pydantic import ValidationError

from app.schemas.code_context_selection import (
    CodeContextSelectionInput,
    CodeContextSelectionResult,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


CODE_CONTEXT_SELECTOR_SYSTEM_PROMPT = """
You are a senior repository context selection agent.

Your only job is to select the best EXISTING repository context for one already-atomic code task.
Return ONLY JSON that matches the provided schema exactly.

You are not generating code.
You are not validating code.
You are not deciding task lifecycle or task status.
You are not proposing files to create.
You are only selecting repository context that ALREADY EXISTS.

Core rule:
- Context selection means selecting files that already exist in the provided repository index.
- If a path is not present in the repository_index, it must NOT be selected.
- Never invent repository paths.
- Never include future output files, intended files to create, or hypothetical files.
- If the task will probably create new files, that is NOT a context selection responsibility.
- In that case, select the best existing context available, even if it is small or empty.

Selection behavior:
- Use only the evidence provided:
  - task payload
  - task hierarchy
  - project operational memory
  - related tasks
  - repository index
  - candidate paths built from existing repository evidence
- Prefer the smallest useful context.
- Distinguish between:
  - primary targets
  - related files
  - reference files
  - related test files
- Use task intent and continuity strongly when supported by existing repository evidence.
- If the project is early-stage or the repo is sparse, it is valid for selected context to be small or empty.
- Missing strong context is a gap to report, not a reason to invent paths.

Output rules:
- Only select paths that exist in repository_index.files.
- candidate_file_pool must contain only paths that exist in repository_index.files.
- Do not output paths that are absent from the repository index.
- If confidence is low, reflect that in confidence_score, context_gaps, and evidence_summary.
- evidence_summary must explain both strengths and weaknesses of the selection.
""".strip()


def _build_selection_user_prompt(selection_input: CodeContextSelectionInput) -> str:
    return f"""
Code context selection input:
{selection_input.model_dump_json(indent=2)}

Important instructions:
- Select only EXISTING repository files.
- Never include paths that are not already present in repository_index.files.
- Never treat intended output files as context.
- If the task appears greenfield or likely to create new files, return the best existing context available, even if small or empty.
- Favor precision over speculation.
- candidate_file_pool should contain only existing repository files that are plausibly useful downstream.
- Make uncertainty explicit in context_gaps and evidence_summary instead of inventing paths.
""".strip()


class CodeContextSelectorClientError(Exception):
    """Base exception for code context selector LLM client."""


def select_code_context_with_model(
    selection_input: CodeContextSelectionInput,
) -> CodeContextSelectionResult:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        CodeContextSelectionResult.model_json_schema()
    )
    user_prompt = _build_selection_user_prompt(selection_input)

    raw = provider.generate_structured(
        system_prompt=CODE_CONTEXT_SELECTOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="code_context_selection_result",
        json_schema=strict_schema,
    )

    try:
        return CodeContextSelectionResult.model_validate(raw)
    except ValidationError as exc:
        raise CodeContextSelectorClientError(
            f"Invalid structured response from code context selector model: {str(exc)}"
        ) from exc
    