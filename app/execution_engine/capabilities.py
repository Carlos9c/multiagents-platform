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
    step_kinds: list[str] = Field(default_factory=list)
    uses_tools: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


class ExecutorCapabilities(BaseModel):
    executor_type: str
    supports_artifact_creation: bool
    supports_artifact_modification: bool
    supports_bootstrap_from_empty_workspace: bool
    requires_workspace: bool = True
    available_actions: list[str] = Field(default_factory=list)
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
        available_actions=[
            "inspect_context",
            "apply_file_operations",
            "run_command",
            "finish",
            "reject",
        ],
        design_guidance=[
            "Prefer atomic tasks that end in a concrete repository or file deliverable.",
            "Prefer tasks whose completion can be assessed from changed files, command output, and workspace evidence.",
            "Keep manual investigation, external research, and human-only validation out of the core task deliverable.",
            "It is acceptable to create or modify multiple related files when they belong to one coherent implementation slice.",
            "Bootstrap from an effectively empty workspace is allowed when the task objective clearly implies a minimal initial structure.",
            "Execution context may include historical task/run context and project memory to preserve consistency.",
            "The implementation agent may decide which files to create or modify when that is necessary to complete the task correctly.",
        ],
        hard_limits=[
            "The orchestrator executes one next action at a time and should prefer the minimum useful step.",
            "run_command is narrow and should be used only when a concrete command is necessary.",
            "Completion phase allows at most one command attempt before the orchestrator is forced to finish or reject.",
            "Validation happens outside the execution engine, so the engine should not treat itself as the final validator.",
            "The current orchestrator routing guarantees only the actions and subagents listed here.",
        ],
        subagents=[
            SubagentCapability(
                name="context_selection_agent",
                role="Selects historical task/run context needed to execute the current atomic task and enriches the execution request.",
                step_kinds=["inspect_context"],
                uses_tools=[
                    "build_context_selection_input",
                ],
                strengths=[
                    "Selects relevant completed historical tasks and runs.",
                    "Uses project context excerpt and historical task catalog for context selection.",
                    "Enriches the execution request before implementation begins.",
                ],
                limits=[
                    "Does not modify files.",
                    "Does not validate task completion.",
                ],
            ),
            SubagentCapability(
                name="code_change_agent",
                role="Implements the task by deciding which files to create or modify and materializing full final contents in the workspace.",
                step_kinds=["apply_file_operations"],
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
                ],
                limits=[
                    "Does not validate task completion.",
                    "Must write only inside the workspace root.",
                    "Must preserve operation integrity: modify for existing files, create for new files.",
                ],
            ),
            SubagentCapability(
                name="command_runner_agent",
                role="Runs one narrow concrete command inside the workspace when the orchestrator explicitly decides it is necessary.",
                step_kinds=["run_command"],
                uses_tools=["run_command"],
                strengths=[
                    "Captures stdout, stderr, and exit code as evidence.",
                    "Useful for one narrow command-based completion check or generation step.",
                ],
                limits=[
                    "Does not decide commands by itself; it only executes the provided command.",
                    "Should not become an open-ended loop of repeated commands.",
                    "Should execute only one narrow command, not shell scripts or chained shell expressions.",
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
                purpose="List repository-relative files under the workspace.",
                notes=[
                    "Used by the implementation agent to understand the current project file surface.",
                    "May return an empty list when bootstrapping from an empty workspace.",
                ],
            ),
            ToolCapability(
                name="read_text_file",
                purpose="Read the current content of a file in the workspace.",
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
                name="run_command",
                purpose="Execute one narrow concrete command in the workspace and capture evidence.",
                notes=[
                    "This is narrow and should be used sparingly.",
                    "It is not for shell scripting, chaining, pipes, or redirection.",
                    "It should only be used when one concrete command is genuinely necessary.",
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


def render_executor_capabilities_for_prompt(executor_type: str | None) -> str:
    capabilities = get_executor_capabilities(executor_type)

    lines: list[str] = [
        f"- executor_type: {capabilities.executor_type}",
        f"- supports_artifact_creation: {capabilities.supports_artifact_creation}",
        f"- supports_artifact_modification: {capabilities.supports_artifact_modification}",
        f"- supports_bootstrap_from_empty_workspace: {capabilities.supports_bootstrap_from_empty_workspace}",
        f"- requires_workspace: {capabilities.requires_workspace}",
    ]

    if capabilities.available_actions:
        lines.append("- available_actions:")
        lines.extend([f"  - {action}" for action in capabilities.available_actions])

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
            if subagent.step_kinds:
                lines.append("    step_kinds:")
                lines.extend([f"      - {item}" for item in subagent.step_kinds])
            if subagent.uses_tools:
                lines.append("    uses_tools:")
                lines.extend([f"      - {item}" for item in subagent.uses_tools])
            if subagent.strengths:
                lines.append("    strengths:")
                lines.extend([f"      - {item}" for item in subagent.strengths])
            if subagent.limits:
                lines.append("    limits:")
                lines.extend([f"      - {item}" for item in subagent.limits])

    if capabilities.tools:
        lines.append("- available_tools:")
        for tool in capabilities.tools:
            lines.append(f"  - name: {tool.name}")
            lines.append(f"    purpose: {tool.purpose}")
            if tool.notes:
                lines.append("    notes:")
                lines.extend([f"      - {item}" for item in tool.notes])

    return "\n".join(lines)
