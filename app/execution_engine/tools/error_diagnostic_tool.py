"""
Error diagnostic tool — two-step LLM-based error analysis.

Step 1 (reference extraction):
  A lightweight LLM call extracts the repository-relative file paths that appear
  verbatim in the failed command's output, plus a one-sentence error signal.
  No regex or pattern matching is used — the LLM parses the trace.

Step 2 (full diagnosis):
  A structured LLM call reads the files referenced in step 1 from the ephemeral
  run tree and produces a full ErrorDiagnosis: error classification, root cause vs
  cascade separation, a concrete repair directive, and the complete set of files
  any repair agent must have access to (newly_discovered_files).

Design contract:
  - The tool describes what went wrong. It does NOT prescribe which subagent repairs it.
  - The orchestrator reads the diagnosis observation to make routing decisions.
  - No limit is placed on the number of files read from the run tree.
  - Per-file content is truncated to guard against binary artifacts.

The run tree MUST still be available (not cleaned up) when run_error_diagnosis() is called.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.error_diagnosis import ErrorDiagnosis
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

# Per-file read limit — guards against binary artifacts and pathologically large files.
# No limit is placed on the NUMBER of files read.
_MAX_FILE_BYTES_PER_FILE = 51_200  # 50 KB

# Maximum characters of stdout/stderr forwarded to each LLM call.
# We keep the tail of the output because errors appear at the end.
_MAX_TRACE_CHARS = 8_000


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

TRACE_REFERENCE_EXTRACTION_SYSTEM_PROMPT = prompt_loader.get(
    "error_diagnostic_tool", "trace_extraction"
)

ERROR_DIAGNOSIS_SYSTEM_PROMPT = prompt_loader.get("error_diagnostic_tool", "diagnosis")


# ---------------------------------------------------------------------------
# Step 1 output schema
# ---------------------------------------------------------------------------


class ErrorTraceReferences(BaseModel):
    """LLM output for step 1: file references and error signal from the raw trace."""

    referenced_files: list[str] = Field(default_factory=list)
    error_signal: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tail(text: str, max_chars: int) -> str:
    """Return the last max_chars characters of text (or the full text if shorter)."""
    if len(text) <= max_chars:
        return text
    return "... [truncated] ...\n" + text[-max_chars:]


def _read_files_from_run_dir(
    run_dir: Path,
    paths: list[str],
) -> dict[str, str]:
    """
    Read file contents from the ephemeral run tree.

    All paths extracted in step 1 are read — there is no limit on file count.
    Per-file content is truncated at _MAX_FILE_BYTES_PER_FILE to handle binary
    artifacts or unexpectedly large generated files.
    """
    run_root = run_dir.resolve()
    contents: dict[str, str] = {}

    for rel_path in paths:
        if not rel_path or not rel_path.strip():
            continue

        clean = rel_path.strip()
        candidate = (run_root / clean).resolve()

        try:
            candidate.relative_to(run_root)
        except ValueError:
            logger.debug(
                "error_diagnostic_tool_path_escapes_run_dir skipping path=%s",
                clean,
            )
            continue

        if not candidate.is_file():
            continue

        try:
            raw_bytes = candidate.read_bytes()
            contents[clean] = raw_bytes[:_MAX_FILE_BYTES_PER_FILE].decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug(
                "error_diagnostic_tool_file_read_failed path=%s error=%s",
                clean,
                str(exc),
            )

    return contents


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_trace_extraction_user_prompt(
    *,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "error_diagnostic_tool",
        "trace_extraction",
        {
            "failed_command": command,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        },
    )
    return f"""Failed command:
- command: {command}
- exit_code: {exit_code}

stdout:
{_tail(stdout, _MAX_TRACE_CHARS) or "[empty]"}

stderr:
{_tail(stderr, _MAX_TRACE_CHARS) or "[empty]"}

Extract the repository-relative file paths referenced in the output and
produce a one-sentence error_signal.
""".strip()


def _render_changed_files_section(changed_files: list) -> str:
    """Format recently changed files for the diagnosis prompt."""
    if not changed_files:
        return "recent_changed_files: [none — no repository changes have been made in this run]"

    lines = ["Recent repository changes (made before this failure):"]
    for cf in changed_files:
        path = getattr(cf, "path", str(cf))
        change_type = getattr(cf, "change_type", "changed")
        producer = getattr(cf, "producer", "unknown")
        lines.append(f"  - {path} ({change_type}, produced by {producer})")
    return "\n".join(lines)


def _build_diagnosis_user_prompt(
    *,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    trace_references: ErrorTraceReferences,
    file_contents: dict[str, str],
    request: ExecutionRequest,
    changed_files: list,
) -> str:
    if file_contents:
        file_section = "\n\n".join(
            f"=== {path} ===\n{content}" for path, content in file_contents.items()
        )
    else:
        file_section = "[no files could be loaded from the run tree]"

    prompt_loader.validate_builder_inputs(
        "error_diagnostic_tool",
        "diagnosis",
        {
            "task_title": request.task_title,
            "task_objective": request.objective,
            "technical_constraints": request.technical_constraints,
            "tests_required": request.tests_required,
            "failed_command": command,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "trace_referenced_files": trace_references.referenced_files,
            "trace_error_signal": trace_references.error_signal,
            "referenced_file_contents": file_section,
            "recent_changed_files": changed_files,
        },
    )
    return f"""Task context:
- title: {request.task_title}
- objective: {request.objective}
- technical_constraints: {request.technical_constraints}
- tests_required: {request.tests_required}

Failed command:
- command: {command}
- exit_code: {exit_code}

stdout:
{_tail(stdout, _MAX_TRACE_CHARS) or "[empty]"}

stderr:
{_tail(stderr, _MAX_TRACE_CHARS) or "[empty]"}

Trace references extracted in step 1:
- referenced_files: {trace_references.referenced_files}
- error_signal: {trace_references.error_signal}

Referenced file contents (from run tree):
{file_section}

{_render_changed_files_section(changed_files)}

Produce the full structured error diagnosis.
""".strip()


# ---------------------------------------------------------------------------
# LLM call wrappers (with one retry each)
# ---------------------------------------------------------------------------


def _extract_trace_references(
    *,
    runtime: BaseAgentRuntime,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> ErrorTraceReferences | None:
    user_prompt = _build_trace_extraction_user_prompt(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )

    raw = runtime.generate_structured(
        system_prompt=TRACE_REFERENCE_EXTRACTION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="error_trace_references",
        json_schema=ErrorTraceReferences.model_json_schema(),
    )

    try:
        return ErrorTraceReferences.model_validate(raw)
    except ValidationError as exc:
        first_error = str(exc)
        logger.warning(
            "error_diagnostic_tool_trace_extraction_invalid_retrying error=%s",
            first_error,
        )

    retry_prompt = (
        f"Your previous output was invalid.\n\n"
        f"Validation error:\n{first_error}\n\n"
        f"Return ONLY a JSON object with exactly:\n"
        f'  "referenced_files": [list of repository-relative paths, may be empty]\n'
        f'  "error_signal": "one sentence root error summary"\n\n'
        f"{user_prompt}"
    )

    retry_raw = runtime.generate_structured(
        system_prompt=TRACE_REFERENCE_EXTRACTION_SYSTEM_PROMPT,
        user_prompt=retry_prompt,
        schema_name="error_trace_references",
        json_schema=ErrorTraceReferences.model_json_schema(),
    )

    try:
        return ErrorTraceReferences.model_validate(retry_raw)
    except ValidationError as retry_exc:
        logger.warning(
            "error_diagnostic_tool_trace_extraction_invalid_after_retry error=%s",
            str(retry_exc),
        )
        return None


def _run_full_diagnosis(
    *,
    runtime: BaseAgentRuntime,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    trace_references: ErrorTraceReferences,
    file_contents: dict[str, str],
    request: ExecutionRequest,
    changed_files: list,
) -> ErrorDiagnosis | None:
    user_prompt = _build_diagnosis_user_prompt(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        trace_references=trace_references,
        file_contents=file_contents,
        request=request,
        changed_files=changed_files,
    )

    raw = runtime.generate_structured(
        system_prompt=ERROR_DIAGNOSIS_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="error_diagnosis",
        json_schema=ErrorDiagnosis.model_json_schema(),
    )

    try:
        return ErrorDiagnosis.model_validate(raw)
    except ValidationError as exc:
        first_error = str(exc)
        logger.warning(
            "error_diagnostic_tool_diagnosis_invalid_retrying error=%s",
            first_error,
        )

    retry_raw = runtime.generate_structured(
        system_prompt=ERROR_DIAGNOSIS_SYSTEM_PROMPT,
        user_prompt=(
            f"Your previous output was invalid.\n\n"
            f"Validation error:\n{first_error}\n\n"
            f"Return only valid JSON matching the ErrorDiagnosis schema.\n\n"
            f"{user_prompt}"
        ),
        schema_name="error_diagnosis",
        json_schema=ErrorDiagnosis.model_json_schema(),
    )

    try:
        return ErrorDiagnosis.model_validate(retry_raw)
    except ValidationError as retry_exc:
        logger.warning(
            "error_diagnostic_tool_diagnosis_invalid_after_retry error=%s",
            str(retry_exc),
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_error_diagnosis(
    *,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    run_dir: Path,
    request: ExecutionRequest,
    runtime: BaseAgentRuntime,
    changed_files: list | None = None,
) -> ErrorDiagnosis | None:
    """
    Run a two-step LLM-based error diagnosis on a failed command execution.

    Step 1 — Reference extraction:
      A lightweight LLM call parses the raw trace and extracts the repository-relative
      file paths mentioned in the output, plus a concise error signal sentence.

    Step 2 — Full diagnosis:
      The referenced files are read from the ephemeral run tree (no file count limit).
      A second LLM call receives the trace, file contents, task context, and the list
      of recently changed files (changed_files) to produce a full ErrorDiagnosis with
      error classification, fault_side, confidence, repair directive, and the complete
      set of files (newly_discovered_files) that any repair agent must have access to.

    changed_files: the list of ChangedFile objects from ExecutionEvidence, used by the
      LLM to determine fault_side (implementation vs. test vs. environment).

    Returns None if any step fails after retries — the caller continues with standard
    error handling in that case.

    The run tree (run_dir) MUST still be available when this function is called.
    """
    logger.info(
        "error_diagnostic_tool_started command=%s exit_code=%s",
        command,
        exit_code,
    )

    # Step 1: extract file references and error signal from the raw trace
    trace_references = _extract_trace_references(
        runtime=runtime,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )

    if trace_references is None:
        logger.warning(
            "error_diagnostic_tool_aborted reason=trace_extraction_failed command=%s",
            command,
        )
        return None

    logger.info(
        "error_diagnostic_tool_trace_extracted referenced_files_count=%s error_signal=%s",
        len(trace_references.referenced_files),
        trace_references.error_signal,
    )

    # Read every referenced file from the run tree (no count limit)
    file_contents = _read_files_from_run_dir(run_dir, trace_references.referenced_files)

    logger.info(
        "error_diagnostic_tool_files_loaded loaded=%s of_referenced=%s paths=%s",
        len(file_contents),
        len(trace_references.referenced_files),
        list(file_contents.keys()),
    )

    # Step 2: full structured diagnosis
    diagnosis = _run_full_diagnosis(
        runtime=runtime,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        trace_references=trace_references,
        file_contents=file_contents,
        request=request,
        changed_files=changed_files or [],
    )

    if diagnosis is None:
        logger.warning(
            "error_diagnostic_tool_aborted reason=diagnosis_failed command=%s",
            command,
        )
        return None

    logger.info(
        "error_diagnostic_tool_completed overall_error_class=%s fault_side=%s "
        "confidence=%s is_recoverable=%s newly_discovered_files_count=%s",
        diagnosis.overall_error_class,
        diagnosis.fault_side,
        diagnosis.confidence,
        diagnosis.is_recoverable,
        len(diagnosis.newly_discovered_files),
    )

    return diagnosis
