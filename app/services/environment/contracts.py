from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

RuntimeType = Literal[
    "python_venv",
    "node_npm",
    "rust_cargo",
    "java_maven",
    "java_gradle",
    "android_gradle",
    "react_native",
    "dotnet",
    "go",
]


class PinnedDependency(BaseModel):
    name: str
    version: str
    extras: list[str] = Field(default_factory=list)


class DependencyRequest(BaseModel):
    name: str
    reason: str
    requested_by: str
    requested_at: str


class SpecChange(BaseModel):
    change_type: Literal["initial", "repair"]
    packages_affected: list[str] = Field(default_factory=list)
    reason: str
    timestamp: str


class RuntimeSpec(BaseModel):
    runtime_type: RuntimeType
    image: str
    dockerfile_path: str | None = None
    dependencies: list[PinnedDependency] = Field(default_factory=list)
    environment_variables: dict[str, str] = Field(default_factory=dict)
    pending_additions: list[DependencyRequest] = Field(default_factory=list)
    change_log: list[SpecChange] = Field(default_factory=list)


@dataclass
class EnvironmentCommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass
class EnvironmentSession:
    project_id: int
    container_id: str
    project_root: Path
    runtime_type: str
    container_mount_point: str = field(default="/project")

    def host_to_container_path(self, host_path: Path) -> str:
        relative = host_path.resolve().relative_to(self.project_root.resolve())
        return f"{self.container_mount_point}/{relative.as_posix()}"


class EnvironmentBootstrapError(Exception):
    pass


class EnvironmentValidationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        spec: RuntimeSpec,
        smoke_test_output: str,
        repair_attempt_output: str | None = None,
    ) -> None:
        self.spec = spec
        self.smoke_test_output = smoke_test_output
        self.repair_attempt_output = repair_attempt_output
        super().__init__(message)
