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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Primary system prompt — test file materialisation
# ---------------------------------------------------------------------------

TEST_BUILDER_AGENT_SYSTEM_PROMPT = """
You are a senior test engineer.

Your job is to write the test files for ONE already-atomic task.
The task's acceptance_criteria are the authoritative specification — write tests that
verify those criteria directly, not tests inferred from observed implementation behaviour.

Core responsibility:
- Produce test files that provide complete, executable coverage of the acceptance_criteria.
- Write tests that will PASS when the implementation is correct and FAIL when it is not.
- If the implementation appears to be missing something required by the acceptance_criteria,
  still write the test that checks for that behaviour — and note it in warnings.
  The test is correct; the implementation gap is a separate concern.
- Match the test structure, naming conventions, and runner already present in the repository.
- Add new test files only when they are truly needed.

Hard rules:
- Write ONLY test files. Do not write implementation/source code, scripts, or configuration
  files — those belong to code_change_agent.
- For operation=create, return the full final test file content.
- For operation=modify, return the full updated test file content.
- Use repository-relative paths only.
- Do not return duplicate paths.
- Test files must be runnable with the project's standard test runner without manual setup.

Test quality expectations:
- Every acceptance criterion must have at least one corresponding test case.
- Test names must clearly describe what they verify.
- Tests must be self-contained: no external state, no network calls unless the task explicitly requires them.
- Use the real implementation symbols — import from actual module paths.
- Mirror the assertion style already present in the repository's existing tests.
- Do not write placeholder or skeleton tests (e.g., assert True, pass).

Coverage philosophy:
- Test the task's acceptance_criteria boundary cases, not every internal detail.
- Prefer meaningful integration-level tests when the acceptance criterion is behavioural.
- Prefer focused unit tests when the acceptance criterion targets an isolated function or class.
- One test file per implementation module is the typical unit; combine only when the task is naturally unified.

Dependency policy:
- Write tests using only packages available in the runtime spec.
- If the task requires a test framework or fixture that is NOT listed in the runtime spec,
  return an empty files list and set needs_dependency to a concise description
  (e.g. "pytest-asyncio>=0.23 — required for async test execution").
- If no runtime spec is provided, use only the standard library or universally available packages.
""".strip()

# ---------------------------------------------------------------------------
# Secondary system prompt — coverage assessment
# ---------------------------------------------------------------------------

COVERAGE_ASSESSMENT_SYSTEM_PROMPT = """
You are a test coverage analyst.

You have just been given the test files written for one atomic task and the task's
acceptance_criteria. Your job is to assess how completely the test suite covers
the acceptance_criteria.

Rules:
- covered_cases: list each acceptance criterion or distinct requirement that is
  directly tested by at least one test case. Be concrete.
- uncovered_cases: list each acceptance criterion or requirement that has NO
  corresponding test case. Be concrete and specific.
- tested_against: classify whether the tests were written against:
  - "acceptance_criteria": tests derive from the stated criteria (preferred)
  - "observed_implementation": tests only describe what the code happens to do
  - "both": a mix of both approaches
- potential_implementation_gaps: list each case where the test targets a criterion
  that the implementation appears to be missing or incorrect. These are cases where
  the test is CORRECT but the implementation likely needs work. Do NOT list normal
  test failures here — only cases where the criterion itself implies the implementation
  is structurally incomplete.
- confidence: your overall confidence in the coverage assessment
  ("high", "medium", or "low").

Important:
- Assess the TEST QUALITY, not the implementation quality.
- potential_implementation_gaps are informational — they do not make the tests wrong.
- Return ONLY JSON matching the provided schema.
""".strip()


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
        "\n".join(f"- {path}" for path in overlay_files)
        if overlay_files
        else "[workspace overlay is currently empty]"
    )
    baseline_section = (
        "\n".join(f"- {path}" for path in baseline_files)
        if baseline_files
        else "[source baseline is currently empty]"
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

    candidates: list[str] = list(request.context.relevant_files)
    historical_context = request.historical_context
    if historical_context is not None:
        for item in historical_context.selected_task_runs:
            candidates.extend(item.changed_files)
            candidates.extend(item.files_read)

    seen: set[str] = set(preloaded.keys())
    if exclude_paths:
        seen.update(exclude_paths)
    selected: list[str] = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        selected.append(path)
    selected = selected[:max_files]

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

    project_context_summary = _build_project_context_summary(request)
    historical_context_summary = _build_historical_context_summary(request)
    runtime_spec_context = _format_runtime_spec_context(request.context.runtime_spec)

    evidence_notes = [
        note.message for note in state.evidence.notes if note.message and note.message.strip()
    ]

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

Implementation files available (source baseline and historical context):
{related_file_context}

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
""".strip()

    return prompt, files_read


def _build_coverage_assessment_prompt(
    *,
    request: ExecutionRequest,
    materialized_files: list[MaterializedFile],
    workspace_root: str,
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
            # Fall back to the content returned by the LLM (in case the file wasn't
            # persisted yet, which shouldn't happen but is a safe fallback)
            test_file_parts.append(mf.content)

    test_files_section = "\n\n".join(test_file_parts) if test_file_parts else "[no test files]"

    # Load implementation context from preloaded_dependency_files
    impl_parts: list[str] = []
    for rel_path, content in request.context.preloaded_dependency_files.items():
        impl_parts.append(f"=== {rel_path} ===\n{content}")
    impl_section = (
        "\n\n".join(impl_parts) if impl_parts else "[no preloaded implementation files available]"
    )

    return f"""
Task acceptance criteria (authoritative specification):
{acceptance_criteria}

Tests required (supplemental guidance):
{tests_required}

Test files just written:
{test_files_section}

Implementation files available in context:
{impl_section}

Assess the coverage of the test suite against the acceptance_criteria.
""".strip()


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

        # One retry with corrective prompt
        retry_raw = self.runtime.generate_structured(
            system_prompt=COVERAGE_ASSESSMENT_SYSTEM_PROMPT,
            user_prompt=(
                f"Your previous output was invalid.\n\n"
                f"Validation error:\n{first_error}\n\n"
                f"Return only valid JSON matching the TestCoverageObservation schema.\n\n"
                f"{user_prompt}"
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

        try:
            materialization = FileMaterializationResult.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "test_builder_agent_invalid_output task_id=%s step_id=%s error=%s",
                request.task_id,
                step.id,
                str(exc),
            )
            raise SubagentRejectedStepError(f"Invalid test builder output: {str(exc)}") from exc

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
            return state

        _validate_generated_files(
            workspace_root=request.context.workspace_path,
            source_root=source_root,
            files=materialization.files,
        )

        ordered_files = sorted(
            materialization.files,
            key=lambda item: (item.path, item.operation),
        )

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

        logger.info(
            "test_builder_agent_completed task_id=%s step_id=%s files_written=%s coverage_assessed=%s",
            request.task_id,
            step.id,
            len(ordered_files),
            coverage is not None,
        )

        return state
