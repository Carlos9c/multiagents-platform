from pydantic import ValidationError

from app.schemas.code_context_selection import (
    CodeContextSelectionInput,
    CodeContextSelectionResult,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


CODE_CONTEXT_SELECTOR_SYSTEM_PROMPT = """
You are a senior repository targeting agent.

Your only job is to select the best available code context for one already-atomic code task.
Return ONLY JSON that matches the provided schema exactly.

You are not generating code.
You are not validating code.
You are not deciding task lifecycle or task status.
You are only selecting repository context as a targeting aid.

Core behavior:
- Always produce the best targeting hypothesis from the available evidence.
- Never treat missing prior execution history as a reason to stop.
- If the project is early-stage or context is sparse, rely more on task intent, task hierarchy, project operational memory, related task memory, and repository file list.
- Keep the selected context as small and useful as possible.
- Distinguish between:
  - primary targets
  - related files
  - reference files
  - related test files

Selection rules:
- Start from the candidate_paths and repository_index.
- Use task intent, hierarchy, project operational context, related tasks, and repository file list.
- Prefer files that align strongly across several signals.
- When continuity exists, use it strongly.
- When it does not exist, do not penalize the task for being new.
- Only select paths that exist in the provided repository index.
- Respect the selection limits in constraints.
- candidate_file_pool should contain the best broader file pool for downstream planning, even if the final selected files are small.

Output rules:
- Do not invent paths outside the repository index.
- If confidence is low, reflect that in confidence_score and context_gaps.
- It is valid for the selected file lists to be small or empty if the task appears to begin new code.
- evidence_summary must explain why the selection is credible and where it is weak.
""".strip()


def _build_selection_user_prompt(selection_input: CodeContextSelectionInput) -> str:
    return f"""
Code context selection input:
{selection_input.model_dump_json(indent=2)}

Important instructions:
- Produce the best targeting hypothesis available from the evidence.
- Do not act as a gatekeeper.
- Missing history is a context gap, not a veto.
- Favor precision, but do not force false certainty.
- candidate_file_pool should remain useful even when final selected files are empty.
- In low-confidence situations, make the uncertainty explicit in context_gaps and evidence_summary.
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