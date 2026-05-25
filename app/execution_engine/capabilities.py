from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.task import EXECUTION_ENGINE


class ToolCapability(BaseModel):
    name: str
    purpose: str
    notes: list[str] = Field(default_factory=list)


class SubagentCapability(BaseModel):
    name: str
    role: str
    uses_tools: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    usage_guidance: list[str] = Field(default_factory=list)


class ExecutorCapabilities(BaseModel):
    executor_type: str
    supports_artifact_creation: bool
    supports_artifact_modification: bool
    supports_bootstrap_from_empty_workspace: bool
    requires_workspace: bool = True
    design_guidance: list[str] = Field(default_factory=list)
    hard_limits: list[str] = Field(default_factory=list)
    subagents: list[SubagentCapability] = Field(default_factory=list)
    tools: list[ToolCapability] = Field(default_factory=list)


def get_execution_engine_capabilities() -> ExecutorCapabilities:
    return ExecutorCapabilities(
        executor_type=EXECUTION_ENGINE,
        supports_artifact_creation=True,
        supports_artifact_modification=True,
        supports_bootstrap_from_empty_workspace=True,
        requires_workspace=True,
        design_guidance=[
            "Prefer atomic tasks that end in a concrete repository or file deliverable.",
            "Prefer tasks whose completion can be assessed from changed files, repository-local verification evidence when it truly adds value, and accumulated workspace evidence.",
            "Keep manual investigation, external research, and human-only validation out of the core operational task deliverable.",
            "It is acceptable to create or modify multiple related files when they belong to one coherent implementation slice.",
            "Bootstrap from an effectively empty workspace is allowed when the task objective clearly implies a minimal initial structure.",
            "Execution context may include historical task/run context and project memory to preserve consistency.",
            "The execution engine should coordinate subagents in the smallest useful sequence needed to complete the operational pass.",
            "Repository-local verification is not required merely because files changed.",
            "Documentation, requirements, specification, README, and design-note tasks often complete once the material document is produced, unless the task explicitly requires an executable repository-local check.",
        ],
        hard_limits=[
            "The orchestrator executes one next decision at a time and should prefer the minimum useful progress.",
            "Validation happens outside the execution engine, so the engine must not treat itself as the final validator.",
            "The current orchestrator routing guarantees only the subagents listed here.",
            "Subagents must operate only within the project execution environment and its controlled runtime abstractions.",
            "Command execution must never be used as open-ended exploration.",
            "A failed verification attempt does not by itself prove that the task still has a concrete product gap.",
        ],
        subagents=[
            SubagentCapability(
                name="context_selection_agent",
                role=(
                    "Prepares or refines execution context by selecting relevant historical task/run information "
                    "and enriching the execution request before or during operational execution."
                ),
                uses_tools=[
                    "build_context_selection_input",
                ],
                strengths=[
                    "Selects relevant completed historical tasks and runs.",
                    "Uses project context excerpt and historical task catalog for context selection.",
                    "Can be used both initially and later if execution reveals a concrete context gap.",
                    "Enriches the execution request without mutating repository files.",
                ],
                limits=[
                    "Does not modify files.",
                    "Does not execute verification commands.",
                    "Does not validate task completion.",
                ],
                usage_guidance=[
                    "Use when the task lacks enough context to proceed safely or coherently.",
                    "Use again during execution only if new work reveals a real context gap.",
                    "Do not use as a delay tactic when a concrete execution step is already clear.",
                ],
            ),
            SubagentCapability(
                name="code_change_agent",
                role=(
                    "Implements the task by deciding which repository files to create or modify "
                    "and materializing full final contents in the execution workspace. "
                    "Handles source code, scripts, configuration files, and migration files. "
                    "Does NOT write test files — those are the responsibility of test_builder_agent."
                ),
                uses_tools=[
                    "list_workspace_files",
                    "read_text_file",
                    "capture_file_snapshot",
                    "restore_file_snapshot",
                    "write_text_file",
                ],
                strengths=[
                    "Can bootstrap a minimal file set from an empty workspace when the task requires it.",
                    "Uses current project structure, historical context, and related files to decide coherent output paths.",
                    "Writes full final file content for create/modify operations.",
                    "Uses snapshots to support rollback on failure.",
                    "Produces repository-local candidate changes that later verification can inspect when verification is materially useful.",
                    "Handles source code, scripts, configuration files, and migration files.",
                    "Can repair implementation gaps identified by error_diagnosis observations.",
                ],
                limits=[
                    "Does not write test files — test file creation and modification is the exclusive responsibility of test_builder_agent.",
                    "Does not validate task completion.",
                    "Does not decide final external acceptance.",
                    "Must write only inside the execution workspace root.",
                    "Must preserve operation integrity: modify for existing files, create for new files.",
                    "Should not be used for tasks whose primary deliverable is a documentation or design artifact — use document_writer_agent instead.",
                    "Should not be used for tasks whose primary goal is writing test files — use test_builder_agent instead.",
                ],
                usage_guidance=[
                    "Use when the task requires creating or modifying source code, scripts, or configuration files.",
                    "Use when an error_diagnosis observation indicates a code-level repair is needed (compilation_failure, import_error, type_error, assertion_error).",
                    "Do not use for tasks whose deliverable is a documentation or design artifact — route those to document_writer_agent.",
                    "Do not use for tasks whose primary goal is writing test files — route those to test_builder_agent.",
                    "Do not use when the current best next step is context recovery or operational verification.",
                ],
            ),
            SubagentCapability(
                name="command_runner_agent",
                role=(
                    "Performs at most one narrow repository-local operational verification step over the candidate run tree "
                    "when executable verification would materially improve confidence in an already-materialized candidate output. "
                    "It decides the concrete verification command and working directory from the accumulated task state "
                    "and the materialized run tree, executes that command, and records structured verification evidence."
                ),
                uses_tools=[
                    "list_workspace_files",
                    "run_command",
                    "materialize_run_tree",
                    "cleanup_run_tree",
                    "read_text_file",
                ],
                strengths=[
                    "Builds an ephemeral candidate run tree from persisted source plus current workspace overlay.",
                    "Chooses a narrow repository-local verification command grounded in the real run-tree inventory.",
                    "Chooses the working directory inside the candidate run tree.",
                    "Captures stdout, stderr, exit code, timeout state, and verification rationale as evidence.",
                    "Improves downstream validation by producing operational proof without requiring validators to execute commands.",
                    "Is most useful for executable code, tests, scripts, endpoints, migrations, and other behavior that can be meaningfully checked from the repository itself.",
                ],
                limits=[
                    "Does not perform open-ended exploration.",
                    "Does not execute shell scripts, chained commands, pipes, or redirection.",
                    "Does not replace implementation work when repository changes are still needed.",
                    "Does not validate final task completion by itself.",
                    "Must operate only on the ephemeral candidate run tree and clean it up afterwards.",
                    "Must not be treated as mandatory after every file change.",
                    "Is usually not the right next step for documentation-only, requirements-only, specification-only, README-only, or design-note-only tasks unless the task explicitly asks for repository-local executable verification.",
                    "If no meaningful repository-local verification exists, it should not be selected.",
                ],
                usage_guidance=[
                    "Use when repository-local operational verification would materially improve confidence in the task outcome.",
                    "Use only when there is already a meaningful candidate implementation or artifact to verify.",
                    "Prefer executable checks whose result would help downstream validation in a concrete way.",
                    "Use for things like targeted tests, linters, builds, type checks, or narrow runtime checks when those are grounded in the repository and task scope.",
                    "Do not use as a substitute for missing context or missing file changes.",
                    "Do not use for exploration or to compensate for uncertainty about whether the task is complete.",
                    "Do not use just because files changed.",
                    "Usually skip this subagent for documentation, requirements, and specification work unless the task explicitly asks for a repository-local executable check.",
                    "Prefer one narrow verification step with clear value for downstream validation.",
                ],
            ),
            SubagentCapability(
                name="test_builder_agent",
                role=(
                    "Writes test files for one atomic task by producing complete, executable "
                    "test coverage of the task's acceptance_criteria. "
                    "Tests are written against the acceptance_criteria, not inferred from "
                    "observed implementation behaviour. "
                    "Performs a secondary coverage assessment (TestCoverageObservation) after "
                    "materialisation and surfaces potential_implementation_gaps as risk flags."
                ),
                uses_tools=[
                    "list_workspace_files",
                    "read_text_file",
                    "capture_file_snapshot",
                    "restore_file_snapshot",
                    "write_text_file",
                ],
                strengths=[
                    "Writes tests derived from acceptance_criteria rather than from observed code.",
                    "Detects potential_implementation_gaps and surfaces them as risk flags.",
                    "Follows existing test structure, naming conventions, and runner configuration.",
                    "Produces a secondary TestCoverageObservation (covered/uncovered/gaps) after materialisation.",
                    "Writes only test files — implementation code is left to code_change_agent.",
                    "Uses snapshots to support rollback on failure.",
                ],
                limits=[
                    "Does not write implementation or source code — test files only.",
                    "Does not execute commands or run the tests.",
                    "Does not validate final task completion — that is handled by the validator.",
                    "Must write only inside the execution workspace root.",
                    "Must preserve operation integrity: modify for existing files, create for new files.",
                    "Should not be used for tasks whose deliverable is source code or documentation.",
                ],
                usage_guidance=[
                    "Use when the task requires writing test files for a testing objective.",
                    "Use for tasks with task_type='testing' or tasks whose acceptance_criteria are best verified by a test suite.",
                    "Do not use for tasks whose deliverable is source code — route those to code_change_agent.",
                    "Do not use for documentation or design artifact tasks — route those to document_writer_agent.",
                    "Do not use for tasks that require running tests — command_runner_agent handles execution.",
                    "When potential_implementation_gaps are flagged, a follow-up code_change_agent call may be needed.",
                ],
            ),
            SubagentCapability(
                name="environment_manager_agent",
                role=(
                    "Installs missing packages or system dependencies into the active runtime "
                    "environment when error_diagnosis.fault_side is 'environment' or "
                    "overall_error_class is 'missing_dependency' with is_recoverable=true. "
                    "Extracts the required packages from the latest error_diagnosis observation, "
                    "runs the install via the runtime's package manager (pip, npm, etc.), and "
                    "updates the RuntimeSpec manifest. Does NOT modify source code or test files."
                ),
                uses_tools=[],
                strengths=[
                    "Resolves ImportError, ModuleNotFoundError, and command_not_found failures "
                    "caused by missing packages without requiring full environment rebuild.",
                    "Follows version_constraint='any_compatible' when called from the orchestrator "
                    "(no user input required).",
                    "Updates RuntimeSpec.dependencies so the installed package is reflected in the manifest.",
                    "Adds structured observations to evidence describing what was installed.",
                    "Supports Python (pip), Node (npm), JVM (maven/gradle), and a generic fallback.",
                ],
                limits=[
                    "Does not modify source code or test files.",
                    "Does not perform full environment teardown/rebuild — use the bootstrapper for that.",
                    "Cannot resolve dependency conflicts that require user input when called from the orchestrator "
                    "(always uses any_compatible).",
                    "On failure (needs_user_input, failed, rolled_back) it raises SubagentRejectedStepError — "
                    "the orchestrator must reject and surface to manual review.",
                    "Requires an active error_diagnosis observation in evidence to identify the packages.",
                ],
                usage_guidance=[
                    "Use when error_diagnosis.fault_side='environment' and is_recoverable=true.",
                    "Use when overall_error_class='missing_dependency' and is_recoverable=true.",
                    "Do not use for code-level errors (assertion, compilation, syntax) — route those to "
                    "code_change_agent instead.",
                    "After environment_manager_agent succeeds, call command_runner_agent to re-verify.",
                ],
            ),
            SubagentCapability(
                name="document_writer_agent",
                role=(
                    "Produces documentation and design artifacts by deciding which files to "
                    "create or modify and materializing their full final contents. "
                    "Handles README, architecture docs, ADRs, API specs, design documents, "
                    "onboarding guides, specification files, and diagram-as-code files. "
                    "May include code snippets inside those documents as examples."
                ),
                uses_tools=[
                    "list_workspace_files",
                    "read_text_file",
                    "capture_file_snapshot",
                    "restore_file_snapshot",
                    "write_text_file",
                ],
                strengths=[
                    "Produces complete, substantive text artifacts in any plain-text format.",
                    "Derives content from task description, objective, and technical constraints.",
                    "Respects existing documentation style and structure in the repository.",
                    "Supports Markdown, YAML, JSON, RST, AsciiDoc, PlantUML, Mermaid, and others.",
                    "Uses snapshots to support rollback on write failure.",
                    "Can embed code snippets inside documentation files as examples or API references.",
                ],
                limits=[
                    "Does not produce source code files meant to be executed or imported.",
                    "Does not execute commands.",
                    "Does not validate final completion — that is handled by the validator.",
                    "Must write only inside the execution workspace root.",
                    "Must preserve operation integrity: modify for existing files, create for new files.",
                ],
                usage_guidance=[
                    "Use for tasks whose primary deliverable is a documentation or design artifact.",
                    "Use for README, architecture docs, ADRs, API specs, onboarding guides, design notes, specification files.",
                    "Do not use for tasks whose deliverable is a source code file, test file, or configuration file meant to be executed or imported — route those to code_change_agent.",
                    "Ensure content is derived from the task context, not invented.",
                ],
            ),
        ],
        tools=[
            ToolCapability(
                name="build_context_selection_input",
                purpose="Build the historical task catalog and project context excerpt used by context selection.",
                notes=[
                    "Skips LLM context selection when no completed historical tasks are available.",
                ],
            ),
            ToolCapability(
                name="list_workspace_files",
                purpose="List repository-relative files under a controlled execution tree.",
                notes=[
                    "Used by execution subagents to understand the current repository file surface.",
                    "May inspect either the editable workspace overlay or the ephemeral run tree depending on the subagent flow.",
                ],
            ),
            ToolCapability(
                name="read_text_file",
                purpose="Read the current content of a file in a controlled execution tree.",
            ),
            ToolCapability(
                name="capture_file_snapshot",
                purpose="Capture pre-write file state before applying changes.",
            ),
            ToolCapability(
                name="restore_file_snapshot",
                purpose="Restore previous file state after a failed materialization attempt.",
            ),
            ToolCapability(
                name="write_text_file",
                purpose="Safely write file content under the workspace root.",
                notes=[
                    "Creates intermediate directories when necessary.",
                    "Rejects writes outside the workspace root.",
                ],
            ),
            ToolCapability(
                name="materialize_run_tree",
                purpose=(
                    "Create the ephemeral candidate run tree by copying persisted source and overlaying "
                    "the current execution workspace changes."
                ),
                notes=[
                    "Used by command_runner_agent before repository-local verification.",
                    "The run tree is disposable and must not become the persisted source tree directly.",
                    "Materializing a run tree does not by itself justify running a command; command execution still requires a meaningful repository-local verification purpose.",
                ],
            ),
            ToolCapability(
                name="cleanup_run_tree",
                purpose="Remove the ephemeral candidate run tree after verification completes or fails.",
                notes=[
                    "Cleanup must not hide the real verification outcome.",
                ],
            ),
            ToolCapability(
                name="run_command",
                purpose="Execute one narrow repository-local command in a controlled working directory and capture structured evidence.",
                notes=[
                    "This is narrow and should be used sparingly.",
                    "It is not for shell scripting, chaining, pipes, or redirection.",
                    "The concrete command should be grounded in the repository candidate tree.",
                    "Its main purpose is to generate operational evidence for external validation.",
                    "It should be used only when an executable repository-local check would materially improve confidence.",
                    "It is usually not appropriate for documentation-only or specification-only tasks unless the task explicitly requests such a check.",
                ],
            ),
        ],
    )


def get_executor_capabilities(executor_type: str | None) -> ExecutorCapabilities:
    if executor_type == EXECUTION_ENGINE:
        return get_execution_engine_capabilities()
    return ExecutorCapabilities(
        executor_type=executor_type or "unknown",
        supports_artifact_creation=False,
        supports_artifact_modification=False,
        supports_bootstrap_from_empty_workspace=False,
        requires_workspace=True,
    )


def get_subagent_capability(
    executor_type: str | None,
    subagent_name: str,
) -> SubagentCapability | None:
    capabilities = get_executor_capabilities(executor_type)
    for subagent in capabilities.subagents:
        if subagent.name == subagent_name:
            return subagent
    return None


def render_executor_capabilities_for_prompt(executor_type: str | None) -> str:
    capabilities = get_executor_capabilities(executor_type)

    lines: list[str] = [
        f"- executor_type: {capabilities.executor_type}",
        f"- supports_artifact_creation: {capabilities.supports_artifact_creation}",
        f"- supports_artifact_modification: {capabilities.supports_artifact_modification}",
        f"- supports_bootstrap_from_empty_workspace: {capabilities.supports_bootstrap_from_empty_workspace}",
        f"- requires_workspace: {capabilities.requires_workspace}",
    ]

    if capabilities.design_guidance:
        lines.append("- task_design_guidance:")
        lines.extend([f"  - {item}" for item in capabilities.design_guidance])

    if capabilities.hard_limits:
        lines.append("- hard_limits:")
        lines.extend([f"  - {item}" for item in capabilities.hard_limits])

    if capabilities.subagents:
        lines.append("- available_subagents:")
        for subagent in capabilities.subagents:
            lines.append(f"  - name: {subagent.name}")
            lines.append(f"    role: {subagent.role}")
            if subagent.uses_tools:
                lines.append("    uses_tools:")
                lines.extend([f"      - {item}" for item in subagent.uses_tools])
            if subagent.strengths:
                lines.append("    strengths:")
                lines.extend([f"      - {item}" for item in subagent.strengths])
            if subagent.limits:
                lines.append("    limits:")
                lines.extend([f"      - {item}" for item in subagent.limits])
            if subagent.usage_guidance:
                lines.append("    usage_guidance:")
                lines.extend([f"      - {item}" for item in subagent.usage_guidance])

    if capabilities.tools:
        lines.append("- available_tools:")
        for tool in capabilities.tools:
            lines.append(f"  - name: {tool.name}")
            lines.append(f"    purpose: {tool.purpose}")
            if tool.notes:
                lines.append("    notes:")
                lines.extend([f"      - {item}" for item in tool.notes])

    return "\n".join(lines)
