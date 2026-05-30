from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

_PROMPTS_ROOT = Path(__file__).parent.parent / "prompts"


class ChangelogEntry(BaseModel):
    version: str
    date: str
    changes: str


class UserPromptInput(BaseModel):
    name: str
    description: str
    required: bool


class PromptEntry(BaseModel):
    description: str
    content: str | None = None
    user_prompt_inputs: list[UserPromptInput] = []


class PromptSpec(BaseModel):
    agent_name: str
    version: str
    changelog: list[ChangelogEntry]
    prompts: dict[str, PromptEntry]


class PromptLoader:
    """Lazy singleton that loads all agent prompt YAML files from app/prompts/.

    YAML files are loaded on the first call to any method. After that the
    registry is cached in-process for the lifetime of the application.

    Usage:
        from app.services.prompt_loader import prompt_loader

        system_prompt = prompt_loader.get("code_change_agent")
        coverage_prompt = prompt_loader.get("test_builder_agent", "coverage_assessment")
    """

    def __init__(self) -> None:
        self._specs: dict[str, PromptSpec] | None = None

    def _ensure_loaded(self) -> None:
        if self._specs is not None:
            return
        specs: dict[str, PromptSpec] = {}
        for yaml_path in sorted(_PROMPTS_ROOT.rglob("*.yaml")):
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            spec = PromptSpec.model_validate(raw)
            if spec.agent_name in specs:
                raise ValueError(f"Duplicate agent_name '{spec.agent_name}' found in {yaml_path}")
            specs[spec.agent_name] = spec
        self._specs = specs

    def get(self, agent_name: str, prompt_key: str = "main") -> str:
        """Return the system prompt content string for the given agent and key.

        Raises KeyError if the key exists but has no content (i.e. it is a
        user-prompt-inputs-only declaration such as a retry key).  Load the
        system prompt from the primary key instead.
        """
        self._ensure_loaded()
        spec = self._specs.get(agent_name)  # type: ignore[union-attr]
        if spec is None:
            raise KeyError(f"No prompt YAML found for agent '{agent_name}'")
        entry = spec.prompts.get(prompt_key)
        if entry is None:
            raise KeyError(f"No prompt key '{prompt_key}' in agent '{agent_name}'")
        if entry.content is None:
            raise KeyError(
                f"Prompt key '{prompt_key}' in agent '{agent_name}' declares only "
                f"user_prompt_inputs — no system prompt content is defined for this key. "
                f"Load the system prompt from the primary key instead."
            )
        return entry.content

    def get_version(self, agent_name: str) -> str:
        """Return the current version string for the given agent."""
        self._ensure_loaded()
        spec = self._specs.get(agent_name)  # type: ignore[union-attr]
        if spec is None:
            raise KeyError(f"No prompt YAML found for agent '{agent_name}'")
        return spec.version

    def get_spec(self, agent_name: str) -> PromptSpec:
        """Return the full PromptSpec for the given agent (used by the Supervisor)."""
        self._ensure_loaded()
        spec = self._specs.get(agent_name)  # type: ignore[union-attr]
        if spec is None:
            raise KeyError(f"No prompt YAML found for agent '{agent_name}'")
        return spec

    def validate_builder_inputs(
        self,
        agent_name: str,
        prompt_key: str,
        inputs: dict,
    ) -> None:
        """Enforce exact-set parity between the inputs dict and the YAML declaration.

        Rules
        -----
        1. Every key present in *inputs* must be declared in the YAML
           ``user_prompt_inputs`` list — no undeclared keys allowed.
        2. Every key declared in ``user_prompt_inputs`` must be present in *inputs* —
           no declared key may be omitted, even if ``required: false``.
        3. ``required: false`` means the *value* may be None/empty; the *key* must
           always appear.

        This enforces full transparency: the Supervisor can read the YAML and know
        exactly what data reaches the LLM for every call, without reading Python source.
        Works for both primary keys (with ``content:``) and user-prompt-inputs-only keys
        (retry/variant keys without ``content:``).

        Call this at the start of every user_prompt builder function.
        """
        self._ensure_loaded()
        spec = self._specs.get(agent_name)  # type: ignore[union-attr]
        if spec is None:
            raise KeyError(f"No prompt YAML found for agent '{agent_name}'")
        entry = spec.prompts.get(prompt_key)
        if entry is None:
            raise KeyError(f"No prompt key '{prompt_key}' in agent '{agent_name}'")

        declared = {inp.name: inp for inp in entry.user_prompt_inputs}
        declared_keys = set(declared.keys())
        input_keys = set(inputs.keys())

        undeclared = input_keys - declared_keys
        if undeclared:
            raise ValueError(
                f"{agent_name}/{prompt_key}: builder usa inputs no declarados en el YAML: "
                f"{sorted(undeclared)}. Añadirlos al YAML y subir la versión."
            )

        missing = declared_keys - input_keys
        if missing:
            raise ValueError(
                f"{agent_name}/{prompt_key}: builder no provee todos los inputs declarados en el YAML: "
                f"{sorted(missing)}. Todo input declarado debe estar presente (puede ser None si required=false)."
            )

    def all_specs(self) -> dict[str, PromptSpec]:
        """Return all loaded specs keyed by agent_name."""
        self._ensure_loaded()
        return dict(self._specs)  # type: ignore[arg-type]


prompt_loader = PromptLoader()
