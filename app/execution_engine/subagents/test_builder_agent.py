from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    CHANGE_TYPE_MODIFIED,
    OBSERVATION_TYPE_DEPENDENCY_REQUIRED,
    OBSERVATION_TYPE_TEST_COVERAGE,
    ExecutionRequest,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.file_operations import (
    FileMaterializationResult,
    MaterializedFile,
)
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.test_coverage import TestCoverageObservation
from app.execution_engine.tools.file_reader_tool import read_text_file
from app.execution_engine.tools.file_snapshot_tool import (
    capture_file_snapshot,
    restore_file_snapshot,
)
from app.execution_engine.tools.file_writer_tool import write_text_file
from app.execution_engine.tools.workspace_scan_tool import list_workspace_files
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_execution_trace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Primary system prompt — test file materialisation
# ---------------------------------------------------------------------------

TEST_BUILDER_AGENT_SYSTEM_PROMPT = prompt_loader.get("test_builder_agent")

# ---------------------------------------------------------------------------
# Secondary system prompt — coverage assessment
# ---------------------------------------------------------------------------

COVERAGE_ASSESSMENT_SYSTEM_PROMPT = prompt_loader.get("test_builder_agent", "coverage_assessment")


# ---------------------------------------------------------------------------
# Helpers shared with code_change_agent (self-contained per subagent pattern)
# ---------------------------------------------------------------------------


def _get_source_root_from_request(request: ExecutionRequest) -> str | None:
    context = request.context
    source_path = getattr(context, "source_path", None)
    if source_path:
        return str(source_path)
    source_dir = getattr(context, "source_dir", None)
    if source_dir:
        return str(source_dir)
    return None


def _resolve_candidate_file_for_read(
    *,
    workspace_root: str,
    source_root: str | None,
    relative_path: str,
) -> Path | None:
    workspace_candidate = (Path(workspace_root).resolve() / relative_path).resolve()
    workspace_root_path = Path(workspace_root).resolve()
    try:
        workspace_candidate.relative_to(workspace_root_path)
    except ValueError as exc:
        raise SubagentRejectedStepError(
            f"Refusing to read file outside workspace boundary: {relative_path}"
        ) from exc

    if workspace_candidate.exists() and workspace_candidate.is_file():
        return workspace_candidate

    if source_root:
        source_root_path = Path(source_root).resolve()
        source_candidate = (source_root_path / relative_path).resolve()
        try:
            source_candidate.relative_to(source_root_path)
        except ValueError as exc:
            raise SubagentRejectedStepError(
                f"Refusing to read file outside source boundary: {relative_path}"
            ) from exc
        if source_candidate.exists() and source_candidate.is_file():
            return source_candidate

    return None


def _candidate_file_exists(
    *,
    workspace_root: str,
    source_root: str | None,
    relative_path: str,
) -> bool:
    return (
        _resolve_candidate_file_for_read(
            workspace_root=workspace_root,
            source_root=source_root,
            relative_path=relative_path,
        )
        is not None
    )


def _format_runtime_spec_context(runtime_spec: dict | None) -> str:
    if not runtime_spec:
        return "[no runtime environment spec available]"
    runtime_type = runtime_spec.get("runtime_type", "unknown")
    image = runtime_spec.get("image", "unknown")
    deps = runtime_spec.get("dependencies", [])
    dep_lines = [
        f"  - {d.get('name', '?')}=={d.get('version', '?')}"
        + (f"[{','.join(d['extras'])}]" if d.get("extras") else "")
        for d in deps
    ]
    lines = [
        f"runtime_type: {runtime_type}",
        f"image: {image}",
        "installed_packages:" if dep_lines else "installed_packages: []",
    ]
    lines.extend(dep_lines)
    return "\n".join(lines)


def _build_historical_context_summary(request: ExecutionRequest) -> str:
    historical_context = request.historical_context
    if historical_context is None or not historical_context.selected_task_runs:
        return "[no historical task context available]"

    parts: list[str] = []
    for item in historical_context.selected_task_runs:
        parts.append(f"- task_id: {item.task_id}")
        parts.append(f"  execution_run_id: {item.execution_run_id}")
        parts.append(f"  title: {item.title}")
        parts.append(f"  selection_rule: {item.selection_rule}")
        parts.append(f"  selection_reason: {item.selection_reason}")
        parts.append(f"  summary: {item.summary}")
        parts.append(f"  objective: {item.objective}")
        parts.append(f"  acceptance_criteria: {item.acceptance_criteria}")
        parts.append(f"  proposed_solution: {item.proposed_solution}")
        parts.append(f"  run_summary: {item.run_summary}")
        parts.append(f"  completed_scope: {item.completed_scope}")
        parts.append(f"  changed_files: {item.changed_files}")
        parts.append(f"  files_read: {item.files_read}")
        parts.append(f"  change_dependencies: {item.change_dependencies}")
    return "\n".join(parts)


def _build_project_context_summary(request: ExecutionRequest) -> str:
    related = [
        {
            "task_id": item.task_id,
            "title": item.title,
            "status": item.status,
            "summary": item.summary,
        }
        for item in request.context.related_tasks
    ]
    source_root = _get_source_root_from_request(request)
    return f"""- relevant_files: {request.context.relevant_files}
- key_decisions: {request.context.key_decisions}
- related_tasks: {related}
- workspace_overlay_root: {request.context.workspace_path}
- source_baseline_root: {source_root}""".strip()


def _paths_to_tree(paths: list[str]) -> str:
    """Render a list of relative posix paths as an indented directory tree."""
    if not paths:
        return ""

    # Build nested dict structure
    tree: dict = {}
    for path in sorted(paths):
        parts = path.split("/")
        node = tree
        for part in parts:
            node = node.setdefault(part, {})

    lines: list[str] = []

    def _render(node: dict, prefix: str) -> None:
        entries = sorted(node.keys())
        for i, name in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            child_prefix = prefix + ("    " if is_last else "│   ")
            lines.append(f"{prefix}{connector}{name}")
            if node[name]:  # has children → it's a directory
                _render(node[name], child_prefix)

    _render(tree, "")
    return "\n".join(lines)


def _build_workspace_inventory_context(
    *,
    workspace_root: str,
    source_root: str | None,
    max_files: int = 500,
) -> str:
    overlay_files = list_workspace_files(workspace_root, max_files=max_files)

    baseline_files: list[str] = []
    if source_root:
        baseline_root = Path(source_root).resolve()
        if baseline_root.exists():
            for path in baseline_root.rglob("*"):
                if path.is_file():
                    baseline_files.append(path.relative_to(baseline_root).as_posix())
            baseline_files = sorted(baseline_files)[:max_files]

    overlay_section = (
        _paths_to_tree(overlay_files) if overlay_files else "[workspace overlay is currently empty]"
    )
    baseline_section = (
        _paths_to_tree(baseline_files) if baseline_files else "[source baseline is currently empty]"
    )

    return (
        "Workspace overlay inventory:\n"
        f"{overlay_section}\n\n"
        "Source baseline inventory:\n"
        f"{baseline_section}"
    )


def _build_related_file_context(
    *,
    workspace_root: str,
    source_root: str | None,
    request: ExecutionRequest,
    exclude_paths: set[str] | None = None,
    max_files: int = 12,
) -> tuple[str, list[tuple[str, str | None]]]:
    preloaded = request.context.preloaded_dependency_files

    parts: list[str] = []
    files_read: list[tuple[str, str | None]] = []

    for rel_path, content in preloaded.items():
        if exclude_paths and rel_path in exclude_paths:
            continue
        parts.append(f"- path: {rel_path}")
        parts.append("  source: preloaded_dependency")
        parts.append("  content:")
        parts.append(content)
        files_read.append((rel_path, "preloaded_dependency"))

    # relevant_files bypass the max_files cap — always loaded regardless of cap
    relevant_candidates: list[str] = list(request.context.relevant_files)
    # Historical files (changed_files + files_read from prior runs) subject to max_files cap
    historical_context = request.historical_context
    historical_candidates: list[str] = []
    if historical_context is not None:
        for item in historical_context.selected_task_runs:
            historical_candidates.extend(item.changed_files)
            historical_candidates.extend(item.files_read)

    seen: set[str] = set(preloaded.keys())
    if exclude_paths:
        seen.update(exclude_paths)

    selected_relevant: list[str] = []
    for path in relevant_candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        selected_relevant.append(path)

    selected_historical: list[str] = []
    for path in historical_candidates:
        if len(selected_historical) >= max_files:
            break
        if not path or path in seen:
            continue
        seen.add(path)
        selected_historical.append(path)

    selected = selected_relevant + selected_historical

    if not selected and not parts:
        return "[no related file content loaded]", []

    for rel_path in selected:
        parts.append(f"- path: {rel_path}")
        resolved = _resolve_candidate_file_for_read(
            workspace_root=workspace_root,
            source_root=source_root,
            relative_path=rel_path,
        )
        if resolved is None:
            parts.append("  source: [missing file]")
            parts.append("  content: [missing file]")
            continue
        try:
            content = read_text_file(str(resolved))
            source_label = "workspace_overlay"
            if source_root:
                source_candidate = (Path(source_root).resolve() / rel_path).resolve()
                if resolved == source_candidate:
                    source_label = "source_baseline"
            files_read.append((rel_path, source_label))
            parts.append(f"  source: {source_label}")
            parts.append("  content:")
            parts.append(content)
        except Exception as exc:
            parts.append(f"  content_error: {str(exc)}")

    return "\n".join(parts), files_read


def _build_current_workspace_state(
    *,
    workspace_root: str,
    state: ResolutionState,
) -> tuple[str, list[str]]:
    seen: set[str] = set()
    unique_paths: list[str] = []
    for cf in state.evidence.changed_files:
        if cf.path not in seen:
            seen.add(cf.path)
            unique_paths.append(cf.path)

    if not unique_paths:
        return "[no files written in this execution run yet]", []

    parts: list[str] = []
    loaded_paths: list[str] = []
    root = Path(workspace_root).resolve()

    for rel_path in unique_paths:
        full_path = (root / rel_path).resolve()
        parts.append(f"- path: {rel_path}")
        if full_path.is_file():
            try:
                content = read_text_file(str(full_path))
                parts.append("  content:")
                parts.append(content)
                loaded_paths.append(rel_path)
            except Exception as exc:
                parts.append(f"  content_error: {str(exc)}")
        else:
            parts.append("  content: [not found in workspace overlay]")

    return "\n".join(parts), loaded_paths


def _scan_existing_test_files(
    *,
    source_root: str | None,
    workspace_root: str,
    exclude_paths: set[str] | None = None,
    max_files: int = 4,
) -> list[str]:
    """
    Returns up to max_files relative paths of existing test files found in the
    source baseline or workspace overlay, prioritising files inside recognised
    test directories. Used to give the agent concrete pattern examples so it
    mirrors the project's existing test conventions instead of inferring them
    from training data.
    """
    _TEST_PATTERNS = (
        "test_*.py",
        "*_test.py",  # Python pytest
        "*.test.js",
        "*.spec.js",  # JS Jest / Mocha
        "*.test.ts",
        "*.spec.ts",  # TS Jest / Vitest
        "*_spec.rb",  # Ruby RSpec
        "*_test.go",  # Go testing
        "*Test.java",  # JUnit
    )
    _TEST_DIR_NAMES = frozenset({"tests", "test", "spec", "__tests__"})

    seen: set[str] = set(exclude_paths or ())
    found: list[str] = []

    for root_str in (source_root, workspace_root):
        if not root_str:
            continue
        root_path = Path(root_str).resolve()
        if not root_path.exists():
            continue
        for pattern in _TEST_PATTERNS:
            for file_path in sorted(root_path.rglob(pattern)):
                if not file_path.is_file():
                    continue
                try:
                    rel = file_path.relative_to(root_path).as_posix()
                except ValueError:
                    continue
                if rel not in seen:
                    seen.add(rel)
                    found.append(rel)

    in_test_dir = [p for p in found if any(part in _TEST_DIR_NAMES for part in p.split("/"))]
    rest = [p for p in found if p not in set(in_test_dir)]
    return (in_test_dir + rest)[:max_files]


def _build_existing_test_context(
    *,
    workspace_root: str,
    source_root: str | None,
    exclude_paths: set[str] | None = None,
    max_files: int = 4,
) -> str:
    """
    Loads the content of representative existing test files so the agent can
    mirror the project's actual test client, import style, and fixture patterns.
    """
    paths = _scan_existing_test_files(
        source_root=source_root,
        workspace_root=workspace_root,
        exclude_paths=exclude_paths,
        max_files=max_files,
    )
    if not paths:
        return "[no existing test files found in repository]"

    parts: list[str] = []
    for rel_path in paths:
        try:
            resolved = _resolve_candidate_file_for_read(
                workspace_root=workspace_root,
                source_root=source_root,
                relative_path=rel_path,
            )
        except SubagentRejectedStepError:
            continue
        parts.append(f"--- {rel_path} ---")
        if resolved is None:
            parts.append("[file not found]")
            continue
        try:
            parts.append(read_text_file(str(resolved)))
        except Exception as exc:
            parts.append(f"[read error: {exc}]")

    return "\n\n".join(parts) if parts else "[no existing test files found in repository]"


# ---------------------------------------------------------------------------
# Infrastructure-file ordering helpers
# ---------------------------------------------------------------------------

_INFRASTRUCTURE_FILENAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "setup.cfg",
        "setup.py",
        "pyproject.toml",
        "spec_helper.rb",
        "rails_helper.rb",
    }
)
_INFRASTRUCTURE_FILE_PREFIXES = ("jest.config.", "vitest.config.", "karma.config.")


def _is_infrastructure_file(path: str) -> bool:
    """Return True if the file is test infrastructure (runner config, shared fixtures, path setup)."""
    filename = Path(path).name
    return filename in _INFRASTRUCTURE_FILENAMES or filename.startswith(
        _INFRASTRUCTURE_FILE_PREFIXES
    )


def _sort_files_infrastructure_first(files: list[MaterializedFile]) -> list[MaterializedFile]:
    """Sort materialised files so infrastructure files precede test files.

    The LLM is instructed to list infrastructure files first, but alphabetical
    sort can violate this (e.g. ``tests/conftest.py`` after ``tests/accounts/``).
    This sort enforces the invariant at the code level.
    """
    return sorted(
        files,
        key=lambda item: (0 if _is_infrastructure_file(item.path) else 1, item.path),
    )


# ---------------------------------------------------------------------------
# Primary-call retry prompt builder
# ---------------------------------------------------------------------------


def _build_primary_call_retry_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    validation_error: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "test_builder_agent",
        "main_retry",
        {
            "task_id": request.task_id,
            "task_title": request.task_title,
            "step_id": step.id,
            "validation_error": validation_error,
        },
    )
    return f"""task_id: {request.task_id}
task_title: {request.task_title}
step_id: {step.id}

Your previous response was invalid.

Validation error:
{validation_error}

Correct your output and return valid JSON matching the FileMaterializationResult schema.
Ensure all required fields are present and values match their declared types.""".strip()


def _validate_generated_files(
    *,
    workspace_root: str,
    source_root: str | None,
    files: list[MaterializedFile],
) -> None:
    if not files:
        raise SubagentRejectedStepError("Test builder agent returned no files to materialise.")

    root = Path(workspace_root).resolve()
    seen_paths: set[str] = set()

    for item in files:
        if not item.path or not item.path.strip():
            raise SubagentRejectedStepError("Test builder agent returned an empty file path.")

        rel_path = item.path.strip()
        if rel_path in seen_paths:
            raise SubagentRejectedStepError(
                f"Duplicate file path returned by test builder agent: {rel_path}"
            )
        seen_paths.add(rel_path)

        destination = (root / rel_path).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise SubagentRejectedStepError(
                f"Refusing to materialise file outside workspace root: {rel_path}"
            ) from exc

        exists_in_candidate_baseline = _candidate_file_exists(
            workspace_root=workspace_root,
            source_root=source_root,
            relative_path=rel_path,
        )

        if item.operation == "modify" and not exists_in_candidate_baseline:
            raise SubagentRejectedStepError(
                f"File '{rel_path}' does not exist in the project candidate baseline, "
                "so operation must be 'create' instead of 'modify'."
            )

        if item.operation == "create" and exists_in_candidate_baseline:
            raise SubagentRejectedStepError(
                f"File '{rel_path}' already exists in the project candidate baseline, "
                "so operation must be 'modify' instead of 'create'."
            )

        if not (item.file_documentation or "").strip():
            raise SubagentRejectedStepError(
                f"Test builder agent returned empty file_documentation for '{rel_path}'. "
                "Every produced file must include a description of what it tests."
            )


# ---------------------------------------------------------------------------
# Context helpers for multi-agent sequences
# ---------------------------------------------------------------------------

_CODE_PRODUCER_AGENTS = frozenset({"code_change_agent"})
_MAX_COMMAND_FAILURE_OUTPUT_CHARS = 1500


def _truncate_for_test_builder(
    text: str, max_chars: int = _MAX_COMMAND_FAILURE_OUTPUT_CHARS
) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]


def _build_current_run_code_changes_context(
    *,
    workspace_root: str,
    state: ResolutionState,
) -> tuple[str, list[tuple[str, str | None]]]:
    """Load implementation files written by code_change_agent in this run.

    In ``code + test`` and ``code + command + test`` sequences the test_builder_agent
    must see the actual implementation it is writing tests for.  Without this, it
    can only see files that were pre-loaded before the run started.
    """
    parts: list[str] = []
    files_read: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    root = Path(workspace_root).resolve()

    for cf in state.evidence.changed_files:
        if cf.producer not in _CODE_PRODUCER_AGENTS:
            continue
        if cf.path in seen:
            continue
        seen.add(cf.path)
        full_path = (root / cf.path).resolve()
        parts.append(f"- path: {cf.path}")
        parts.append(f"  producer: {cf.producer}")
        if full_path.is_file():
            try:
                content = read_text_file(str(full_path))
                parts.append("  content:")
                parts.append(content)
                files_read.append((cf.path, "workspace_overlay"))
            except Exception as exc:
                parts.append(f"  content_error: {str(exc)}")
        else:
            parts.append("  content: [not found in workspace overlay]")

    if not parts:
        return "[no implementation files written by code_change_agent in this run]", []

    return "\n".join(parts), files_read


def _build_last_command_failure_summary(state: ResolutionState) -> str:
    """Format the most recent failed command output for use in test_builder repair passes.

    In a repair pass (materialization_attempt_count > 0) the agent needs to know
    exactly WHAT failed — a ModuleNotFoundError requires conftest.py (infrastructure),
    an AssertionError requires test logic changes, a TypeError in call signatures
    requires updating mocks.  The notes text alone is too vague for this distinction.
    """
    if not state.evidence.commands:
        return "[no commands executed in this run]"

    for cmd in reversed(state.evidence.commands):
        if cmd.timed_out or cmd.exit_code != 0:
            lines = [
                f"command: {cmd.command}",
                f"exit_code: {cmd.exit_code}",
                f"timed_out: {cmd.timed_out}",
            ]
            if cmd.stdout:
                lines.append(f"stdout:\n{_truncate_for_test_builder(cmd.stdout)}")
            if cmd.stderr:
                lines.append(f"stderr:\n{_truncate_for_test_builder(cmd.stderr)}")
            if cmd.observed_outcome_summary:
                lines.append(f"observed_outcome_summary: {cmd.observed_outcome_summary}")
            if cmd.verification_goal:
                lines.append(f"verification_goal: {cmd.verification_goal}")
            return "\n".join(lines)

    return "[all commands succeeded — no failure output available]"


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_user_prompt(
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
) -> tuple[str, list[tuple[str, str | None]]]:
    source_root = _get_source_root_from_request(request)

    workspace_inventory = _build_workspace_inventory_context(
        workspace_root=request.context.workspace_path,
        source_root=source_root,
    )
    current_workspace_state, workspace_state_paths = _build_current_workspace_state(
        workspace_root=request.context.workspace_path,
        state=state,
    )
    related_file_context, files_read = _build_related_file_context(
        workspace_root=request.context.workspace_path,
        source_root=source_root,
        request=request,
        exclude_paths=set(workspace_state_paths),
    )
    existing_test_context = _build_existing_test_context(
        workspace_root=request.context.workspace_path,
        source_root=source_root,
        exclude_paths=set(workspace_state_paths),
    )
    # Gap #2: implementation files written by code_change_agent in this run — critical
    # for code+test sequences where tests must cover freshly created implementation.
    code_change_files_context, code_change_files_read = _build_current_run_code_changes_context(
        workspace_root=request.context.workspace_path,
        state=state,
    )
    for path, source_label in code_change_files_read:
        state.evidence.add_file_read(
            path=path,
            producer="test_builder_agent",
            source=source_label,
        )
    # Gap #4: structured failure output — only useful on repair passes.
    # Note: increment_materialization_attempts() is called before _build_user_prompt,
    # so count==1 on the first attempt and >=2 on subsequent repair passes.
    last_command_failure_summary = (
        _build_last_command_failure_summary(state)
        if state.materialization_attempt_count > 1
        else "[not applicable — first materialisation attempt]"
    )

    project_context_summary = _build_project_context_summary(request)
    historical_context_summary = _build_historical_context_summary(request)
    runtime_spec_context = _format_runtime_spec_context(request.context.runtime_spec)

    evidence_notes = [
        note.message for note in state.evidence.notes if note.message and note.message.strip()
    ]

    prompt_loader.validate_builder_inputs(
        "test_builder_agent",
        "main",
        {
            "task_id": request.task_id,
            "task_title": request.task_title,
            "task_description": request.task_description,
            "task_summary": request.task_summary,
            "objective": request.objective,
            "proposed_solution": request.proposed_solution,
            "implementation_notes": request.implementation_notes,
            "implementation_steps": request.implementation_steps,
            "acceptance_criteria": request.acceptance_criteria,
            "tests_required": request.tests_required,
            "technical_constraints": request.technical_constraints,
            "out_of_scope": request.out_of_scope,
            "executor_type": request.executor_type,
            "step_id": step.id,
            "step_title": step.title,
            "step_instructions": step.instructions,
            "step_target_paths": step.target_paths,
            "runtime_spec_context": runtime_spec_context,
            "project_context_summary": project_context_summary,
            "historical_context_summary": historical_context_summary,
            "workspace_inventory": workspace_inventory,
            "current_workspace_state": current_workspace_state,
            "implementation_files": related_file_context,
            "existing_test_context": existing_test_context,
            "code_change_agent_files_context": code_change_files_context,
            "last_command_failure_summary": last_command_failure_summary,
            "orchestration_phase": state.phase,
            "materialization_attempt_count": state.materialization_attempt_count,
            "risk_flags": state.risk_flags,
            "step_notes": state.step_notes,
            "evidence_notes": evidence_notes,
        },
    )
    prompt = f"""
Task:
- task_id: {request.task_id}
- title: {request.task_title}
- description: {request.task_description}
- summary: {request.task_summary}
- objective: {request.objective}
- proposed_solution: {request.proposed_solution}
- implementation_notes: {request.implementation_notes}
- implementation_steps: {request.implementation_steps}
- acceptance_criteria: {request.acceptance_criteria}
- tests_required: {request.tests_required}
- technical_constraints: {request.technical_constraints}
- out_of_scope: {request.out_of_scope}
- executor_type: {request.executor_type}

Current subagent step:
- step_id: {step.id}
- step_title: {step.title}
- step_instructions: {step.instructions}
- target_paths: {step.target_paths}

Runtime environment:
{runtime_spec_context}

Project context:
{project_context_summary}

Historical task context (implementation context — use to understand the code under test):
{historical_context_summary}

Repository inventory:
{workspace_inventory}

Current workspace state (files written in this run — authoritative current content for repair passes):
{current_workspace_state}

Implementation files written by code_change_agent in this run (authoritative — use as primary source for import paths and API signatures):
{code_change_files_context}

Implementation files available (source baseline and historical context):
{related_file_context}

Existing test patterns in this repository (mirror these conventions — import style, test client, fixture setup):
{existing_test_context}

Last command failure output (use on repair passes to diagnose the exact fix needed):
{last_command_failure_summary}

Current orchestration state:
- phase: {state.phase}
- materialization_attempt_count: {state.materialization_attempt_count}
- risk_flags: {state.risk_flags}
- step_notes: {state.step_notes}
- evidence_notes: {evidence_notes}

Quality expectations:
- Write tests that verify acceptance_criteria directly.
- Do not test internal implementation details not required by acceptance_criteria.
- Match the test structure and conventions already present in the repository.
- Ensure every test is executable with the project's standard test runner.
- When the current workspace state is non-empty, treat it as ground truth for existing test contents.
- When code_change_agent_files_context is non-empty, use those files as the authoritative
  source for module paths, function signatures, and class interfaces to test against.
""".strip()

    return prompt, files_read


_IMPL_WRITING_AGENTS = frozenset({"code_change_agent", "document_writer_agent"})


def _build_coverage_assessment_prompt(
    *,
    request: ExecutionRequest,
    materialized_files: list[MaterializedFile],
    workspace_root: str,
    source_root: str | None = None,
    state: ResolutionState | None = None,
) -> str:
    acceptance_criteria = request.acceptance_criteria or "[not specified]"
    tests_required = request.tests_required or "[not specified]"

    # Load the just-written test files from the workspace
    test_file_parts: list[str] = []
    root = Path(workspace_root).resolve()
    for mf in materialized_files:
        full_path = (root / mf.path).resolve()
        test_file_parts.append(f"=== {mf.path} ===")
        if full_path.is_file():
            try:
                test_file_parts.append(read_text_file(str(full_path)))
            except Exception as exc:
                test_file_parts.append(f"[read error: {exc}]")
        else:
            # Fall back to the content returned by the LLM
            test_file_parts.append(mf.content)

    test_files_section = "\n\n".join(test_file_parts) if test_file_parts else "[no test files]"

    # Build implementation context from multiple sources
    impl_parts: list[str] = []
    seen_impl: set[str] = set()

    # 1. Preloaded dependency files
    for rel_path, content in request.context.preloaded_dependency_files.items():
        impl_parts.append(f"=== {rel_path} (preloaded) ===\n{content}")
        seen_impl.add(rel_path)

    # 2. Relevant files — authoritative; read from workspace/source
    for rel_path in request.context.relevant_files:
        if not rel_path or rel_path in seen_impl:
            continue
        seen_impl.add(rel_path)
        try:
            resolved = _resolve_candidate_file_for_read(
                workspace_root=workspace_root,
                source_root=source_root,
                relative_path=rel_path,
            )
            if resolved is not None and resolved.is_file():
                impl_parts.append(
                    f"=== {rel_path} (relevant_file) ===\n{read_text_file(str(resolved))}"
                )
        except Exception:
            pass

    # 3. Files written by writing agents in this run (code_change_agent, document_writer_agent)
    if state is not None:
        for cf in state.evidence.changed_files:
            if cf.producer not in _IMPL_WRITING_AGENTS:
                continue
            if cf.path in seen_impl:
                continue
            seen_impl.add(cf.path)
            full_path = (root / cf.path).resolve()
            if full_path.is_file():
                try:
                    impl_parts.append(
                        f"=== {cf.path} (written_by_{cf.producer}) ===\n"
                        f"{read_text_file(str(full_path))}"
                    )
                except Exception:
                    pass

    impl_section = "\n\n".join(impl_parts) if impl_parts else "[no implementation files available]"

    # Summarise relevant_files and other-agent changes for the prompt header
    relevant_files_context = (
        ", ".join(request.context.relevant_files)
        if request.context.relevant_files
        else "[not specified]"
    )
    other_agent_changes_lines: list[str] = []
    if state is not None:
        for cf in state.evidence.changed_files:
            if cf.producer in _IMPL_WRITING_AGENTS:
                other_agent_changes_lines.append(f"- {cf.path} (by {cf.producer})")
    other_agent_changes_context = (
        "\n".join(other_agent_changes_lines) if other_agent_changes_lines else "[none in this run]"
    )

    prompt_loader.validate_builder_inputs(
        "test_builder_agent",
        "coverage_assessment",
        {
            "acceptance_criteria": acceptance_criteria,
            "tests_required": tests_required,
            "materialized_test_files": test_files_section,
            "implementation_files_context": impl_section,
            "relevant_files_context": relevant_files_context,
            "other_agent_changes_context": other_agent_changes_context,
        },
    )
    return f"""
Task acceptance criteria (authoritative specification):
{acceptance_criteria}

Tests required (supplemental guidance):
{tests_required}

Relevant files (most important files for this task):
{relevant_files_context}

Implementation files changed by other agents in this run:
{other_agent_changes_context}

Test files just written:
{test_files_section}

Implementation files available in context:
{impl_section}

Assess the coverage of the test suite against the acceptance_criteria.
""".strip()


# ---------------------------------------------------------------------------
# Trace writer
# ---------------------------------------------------------------------------


def _write_test_builder_trace(
    *,
    request: ExecutionRequest,
    step_id: str,
    call_type: str,
    files_written: list[dict],
    coverage: "TestCoverageObservation | None",
    needs_dependency: str | None = None,
) -> None:
    entry: dict = {
        "agent": "test_builder_agent",
        "project_id": request.project_id,
        "task_id": request.task_id,
        "run_id": request.execution_run_id,
        "step_id": step_id,
        "call_type": call_type,
        "files_written": files_written,
    }
    if needs_dependency:
        entry["needs_dependency"] = needs_dependency
    if coverage is not None:
        entry["coverage_summary"] = {
            "covered_count": len(coverage.covered_cases),
            "uncovered_count": len(coverage.uncovered_cases),
            "confidence": coverage.confidence,
            "tested_against": coverage.tested_against,
            "covered_cases": coverage.covered_cases,
            "uncovered_cases": coverage.uncovered_cases,
            "potential_implementation_gaps": coverage.potential_implementation_gaps,
        }
    append_execution_trace(project_id=request.project_id, entry=entry)


# ---------------------------------------------------------------------------
# SubAgent class
# ---------------------------------------------------------------------------


class TestBuilderAgent(BaseSubagent):
    name = "test_builder_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime

    def _run_coverage_assessment(
        self,
        *,
        request: ExecutionRequest,
        materialized_files: list[MaterializedFile],
        workspace_root: str,
        source_root: str | None = None,
        state: ResolutionState | None = None,
    ) -> TestCoverageObservation | None:
        """
        Secondary LLM call: assess what the just-written tests actually cover
        relative to the task's acceptance_criteria.

        Returns None on failure so materialisation is never rolled back
        due to a coverage assessment error.
        """
        user_prompt = _build_coverage_assessment_prompt(
            request=request,
            materialized_files=materialized_files,
            workspace_root=workspace_root,
            source_root=source_root,
            state=state,
        )

        raw = self.runtime.generate_structured(
            system_prompt=COVERAGE_ASSESSMENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="test_builder_agent_coverage_assessment",
            json_schema=TestCoverageObservation.model_json_schema(),
        )

        first_error: str | None = None
        try:
            return TestCoverageObservation.model_validate(raw)
        except ValidationError as exc:
            first_error = str(exc)
            logger.warning(
                "test_builder_agent_coverage_assessment_invalid task_id=%s error=%s retrying=true",
                request.task_id,
                first_error,
            )

        # One compact retry — only acceptance_criteria + error; no full prompt duplication
        retry_raw = self.runtime.generate_structured(
            system_prompt=COVERAGE_ASSESSMENT_SYSTEM_PROMPT,
            user_prompt=(
                f"acceptance_criteria: {request.acceptance_criteria or '[not specified]'}\n\n"
                f"Your previous response was invalid.\n"
                f"Validation error: {first_error}\n\n"
                f"Return ONLY valid JSON matching the TestCoverageObservation schema."
            ),
            schema_name="test_builder_agent_coverage_assessment",
            json_schema=TestCoverageObservation.model_json_schema(),
        )

        try:
            return TestCoverageObservation.model_validate(retry_raw)
        except ValidationError as retry_exc:
            logger.warning(
                "test_builder_agent_coverage_assessment_invalid_after_retry task_id=%s error=%s",
                request.task_id,
                str(retry_exc),
            )
            return None

    def execute_step(
        self,
        *,
        db: Session,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if step.subagent_name != self.name:
            raise SubagentRejectedStepError(
                f"{self.name} received a step for subagent '{step.subagent_name}'."
            )

        state.increment_materialization_attempts()

        logger.info(
            "test_builder_agent_started task_id=%s step_id=%s attempt=%s",
            request.task_id,
            step.id,
            state.materialization_attempt_count,
        )

        source_root = _get_source_root_from_request(request)

        user_prompt, files_read = _build_user_prompt(request, step, state)
        for path, source_label in files_read:
            state.evidence.add_file_read(
                path=path,
                producer=self.name,
                source=source_label,
            )

        raw = self.runtime.generate_structured(
            system_prompt=TEST_BUILDER_AGENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="test_builder_agent_file_materialisation",
            json_schema=FileMaterializationResult.model_json_schema(),
        )

        first_error: str | None = None
        try:
            materialization = FileMaterializationResult.model_validate(raw)
        except ValidationError as exc:
            first_error = str(exc)
            logger.warning(
                "test_builder_agent_invalid_output task_id=%s step_id=%s error=%s retrying=true",
                request.task_id,
                step.id,
                first_error,
            )

        if first_error is not None:
            retry_user_prompt = _build_primary_call_retry_prompt(
                request=request,
                step=step,
                validation_error=first_error,
            )
            retry_raw = self.runtime.generate_structured(
                system_prompt=TEST_BUILDER_AGENT_SYSTEM_PROMPT,
                user_prompt=retry_user_prompt,
                schema_name="test_builder_agent_file_materialisation",
                json_schema=FileMaterializationResult.model_json_schema(),
            )
            try:
                materialization = FileMaterializationResult.model_validate(retry_raw)
            except ValidationError as retry_exc:
                logger.warning(
                    "test_builder_agent_invalid_output_after_retry task_id=%s step_id=%s error=%s",
                    request.task_id,
                    step.id,
                    str(retry_exc),
                )
                raise SubagentRejectedStepError(
                    f"Invalid test builder output after retry: {str(retry_exc)}"
                ) from retry_exc

        logger.info(
            "test_builder_agent_files_planned task_id=%s step_id=%s file_count=%s",
            request.task_id,
            step.id,
            len(materialization.files),
        )

        # Dependency signal — same contract as code_change_agent
        if materialization.needs_dependency:
            logger.warning(
                "test_builder_agent_needs_dependency task_id=%s step_id=%s reason=%s",
                request.task_id,
                step.id,
                materialization.needs_dependency,
            )
            state.set_needs_dependency_signal(materialization.needs_dependency)
            state.evidence.add_note(
                message=f"needs_dependency: {materialization.needs_dependency}",
                producer=self.name,
            )
            state.evidence.add_observation(
                evidence_type=OBSERVATION_TYPE_DEPENDENCY_REQUIRED,
                producer=self.name,
                summary=f"Dependency required: {materialization.needs_dependency}",
                payload={"package_description": materialization.needs_dependency},
            )
            _write_test_builder_trace(
                request=request,
                step_id=step.id,
                call_type="needs_dependency",
                files_written=[],
                coverage=None,
                needs_dependency=materialization.needs_dependency,
            )
            return state

        _validate_generated_files(
            workspace_root=request.context.workspace_path,
            source_root=source_root,
            files=materialization.files,
        )

        ordered_files = _sort_files_infrastructure_first(materialization.files)

        snapshot_paths = [item.path for item in ordered_files]
        snapshots = capture_file_snapshot(
            root_dir=request.context.workspace_path,
            relative_paths=snapshot_paths,
        )

        try:
            for generated in ordered_files:
                absolute_path = write_text_file(
                    root_dir=request.context.workspace_path,
                    relative_path=generated.path,
                    content=generated.content,
                )

                change_type = (
                    CHANGE_TYPE_CREATED if generated.operation == "create" else CHANGE_TYPE_MODIFIED
                )

                logger.info(
                    "test_builder_agent_file_written task_id=%s path=%s operation=%s",
                    request.task_id,
                    generated.path,
                    generated.operation,
                )

                state.evidence.add_changed_file(
                    path=generated.path,
                    change_type=change_type,
                    producer=self.name,
                )
                state.evidence.add_file_documentation(
                    path=generated.path,
                    documentation=generated.file_documentation,
                    change_summary=generated.rationale,
                    agent=self.name,
                    operation=generated.operation,
                )
                state.evidence.add_note(
                    message=f"Wrote test file {generated.path} at {absolute_path}",
                    producer=self.name,
                )
                state.evidence.add_note(
                    message=f"Rationale for {generated.path}: {generated.rationale}",
                    producer=self.name,
                )

        except Exception:
            logger.exception(
                "test_builder_agent_write_failed task_id=%s step_id=%s rolling_back=true",
                request.task_id,
                step.id,
            )
            restore_file_snapshot(
                root_dir=request.context.workspace_path,
                snapshots=snapshots,
            )
            raise

        for note in materialization.notes:
            state.evidence.add_note(message=note, producer=self.name)

        state.add_risk_flags(materialization.warnings)

        # Secondary LLM call: assess test coverage against acceptance_criteria
        coverage = self._run_coverage_assessment(
            request=request,
            materialized_files=ordered_files,
            workspace_root=request.context.workspace_path,
            source_root=source_root,
            state=state,
        )

        if coverage is not None:
            state.evidence.add_observation(
                evidence_type=OBSERVATION_TYPE_TEST_COVERAGE,
                producer=self.name,
                summary=(
                    f"Test coverage: {len(coverage.covered_cases)} covered, "
                    f"{len(coverage.uncovered_cases)} uncovered, "
                    f"confidence={coverage.confidence}"
                ),
                payload=coverage.model_dump(mode="json"),
            )
            if coverage.potential_implementation_gaps:
                gap_flags = [
                    f"potential_implementation_gap:{gap}"
                    for gap in coverage.potential_implementation_gaps
                ]
                state.add_risk_flags(gap_flags)
                state.evidence.add_note(
                    message=(
                        f"Test coverage assessment detected "
                        f"{len(coverage.potential_implementation_gaps)} potential "
                        f"implementation gap(s): {coverage.potential_implementation_gaps}"
                    ),
                    producer=self.name,
                )

        state.add_note(f"Test materialisation completed for {len(ordered_files)} file(s).")

        _write_test_builder_trace(
            request=request,
            step_id=step.id,
            call_type="materialise",
            files_written=[
                {"path": f.path, "operation": f.operation, "rationale": f.rationale}
                for f in ordered_files
            ],
            coverage=coverage,
        )

        logger.info(
            "test_builder_agent_completed task_id=%s step_id=%s files_written=%s coverage_assessed=%s",
            request.task_id,
            step.id,
            len(ordered_files),
            coverage is not None,
        )

        return state
