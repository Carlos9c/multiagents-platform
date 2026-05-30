"""
Resolve the exact system prompt content that was active at a given git commit.

The Supervisor uses this to show evaluators the same prompt the evaluated agent
received, enabling fair assessment of the agent's behaviour against its guidance.

Strategy:
1. Try `git show <commit>:app/prompts/<subdir>/<agent_name>.yaml` to read the
   file as it existed at that commit.
2. Parse the YAML and extract the requested prompt key.
3. If the commit is "unknown", "-dirty", or git fails, fall back to the current
   prompt_loader (best-effort).
"""

from __future__ import annotations

import logging
import subprocess

import yaml

logger = logging.getLogger(__name__)

# Mapping of agent_name → relative path inside app/prompts/
_AGENT_YAML_PATHS: dict[str, str] = {
    "planner": "planning/planner.yaml",
    "atomic_task_generator": "planning/atomic_task_generator.yaml",
    "execution_sequencer": "planning/execution_sequencer.yaml",
    "environment_planner": "environment/environment_planner.yaml",
    "catalog_selector": "environment/catalog_selector.yaml",
    "planner_evaluator": "supervisor/planner_evaluator.yaml",
    "atomic_task_generator_evaluator": "supervisor/atomic_task_generator_evaluator.yaml",
    "execution_sequencer_evaluator": "supervisor/execution_sequencer_evaluator.yaml",
    "environment_planner_evaluator": "supervisor/environment_planner_evaluator.yaml",
    "catalog_selector_evaluator": "supervisor/catalog_selector_evaluator.yaml",
    "orchestrator_evaluator": "supervisor/orchestrator_evaluator.yaml",
    "context_selection_agent_evaluator": "supervisor/context_selection_agent_evaluator.yaml",
    "environment_manager_agent_evaluator": "supervisor/environment_manager_agent_evaluator.yaml",
    "code_change_agent_evaluator": "supervisor/code_change_agent_evaluator.yaml",
    "code_change_agent_validator_evaluator": "supervisor/code_change_agent_validator_evaluator.yaml",
    "command_runner_agent_evaluator": "supervisor/command_runner_agent_evaluator.yaml",
    "command_runner_agent_validator_evaluator": "supervisor/command_runner_agent_validator_evaluator.yaml",
    "test_builder_agent_evaluator": "supervisor/test_builder_agent_evaluator.yaml",
    "test_builder_agent_validator_evaluator": "supervisor/test_builder_agent_validator_evaluator.yaml",
    "document_writer_agent_evaluator": "supervisor/document_writer_agent_evaluator.yaml",
    "document_writer_agent_validator_evaluator": "supervisor/document_writer_agent_validator_evaluator.yaml",
    "stage_evaluator_evaluator": "supervisor/stage_evaluator_evaluator.yaml",
    "recovery_planner_evaluator": "supervisor/recovery_planner_evaluator.yaml",
    "recovery_assignment_evaluator": "supervisor/recovery_assignment_evaluator.yaml",
    "requirements_evaluator_evaluator": "supervisor/requirements_evaluator_evaluator.yaml",
    "review_episode_evaluator": "supervisor/review_episode_evaluator.yaml",
    "aria_conversation_evaluator": "supervisor/aria_conversation_evaluator.yaml",
}


def resolve_system_prompt(
    agent_name: str,
    prompt_key: str = "main",
    *,
    system_version: str | None = None,
) -> str:
    """Return the system prompt content for *agent_name*/*prompt_key*.

    If *system_version* is a valid (non-dirty, non-unknown) git commit hash,
    attempt to read the YAML at that historical revision.  Falls back to the
    current in-process prompt_loader on any failure.
    """
    from app.services.prompt_loader import prompt_loader  # late import to avoid cycles

    if system_version and _is_resolvable_commit(system_version):
        content = _git_show_prompt(
            agent_name=agent_name,
            prompt_key=prompt_key,
            commit=system_version,
        )
        if content is not None:
            return content

    # Fallback: current prompt
    return prompt_loader.get(agent_name, prompt_key)


def _is_resolvable_commit(version: str) -> bool:
    """Return True if the version looks like a resolvable git commit hash."""
    if version in {"unknown", ""}:
        return False
    # Accept both clean (40-char hex) and dirty (hash-dirty) variants
    commit = version.split("-dirty")[0]
    return len(commit) >= 7 and all(c in "0123456789abcdefABCDEF" for c in commit)


def _git_show_prompt(
    agent_name: str,
    prompt_key: str,
    commit: str,
) -> str | None:
    """Run `git show <commit>:app/prompts/<path>` and parse the prompt content.

    Returns None on any failure so the caller can fall back.
    """
    yaml_relative = _AGENT_YAML_PATHS.get(agent_name)
    if yaml_relative is None:
        logger.debug("prompt_resolver: no YAML path registered for agent '%s'", agent_name)
        return None

    # Strip -dirty suffix for git show
    clean_commit = commit.split("-dirty")[0]
    git_path = f"app/prompts/{yaml_relative}"

    try:
        result = subprocess.run(
            ["git", "show", f"{clean_commit}:{git_path}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.debug(
                "prompt_resolver: git show failed for %s@%s: %s",
                agent_name,
                clean_commit,
                result.stderr.strip(),
            )
            return None

        raw = yaml.safe_load(result.stdout)
        if not isinstance(raw, dict):
            return None

        prompts = raw.get("prompts", {})
        entry = prompts.get(prompt_key, {})
        content = entry.get("content")
        if not isinstance(content, str):
            return None
        return content

    except Exception:
        logger.warning(
            "prompt_resolver: unexpected error resolving %s@%s",
            agent_name,
            clean_commit,
            exc_info=True,
        )
        return None
