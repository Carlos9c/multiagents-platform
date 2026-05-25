from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Error class constants
# ---------------------------------------------------------------------------

ERROR_CLASS_MISSING_DEPENDENCY = "missing_dependency"
ERROR_CLASS_COMPILATION_FAILURE = "compilation_failure"
ERROR_CLASS_BUILD_FAILURE = "build_failure"
ERROR_CLASS_STARTUP_FAILURE = "startup_failure"
ERROR_CLASS_ASSERTION_FAILURE = "assertion_failure"
ERROR_CLASS_LINT_VIOLATION = "lint_violation"
ERROR_CLASS_MIGRATION_FAILURE = "migration_failure"
ERROR_CLASS_RUNTIME_ERROR = "runtime_error"
ERROR_CLASS_COMMAND_NOT_FOUND = "command_not_found"
ERROR_CLASS_PERMISSION_ERROR = "permission_error"
ERROR_CLASS_TIMEOUT = "timeout"
ERROR_CLASS_NETWORK_ERROR = "network_error"
ERROR_CLASS_CONFIGURATION_ERROR = "configuration_error"
ERROR_CLASS_SCRIPT_ERROR = "script_error"
ERROR_CLASS_UNKNOWN = "unknown"

ErrorClassLiteral = Literal[
    "missing_dependency",
    "compilation_failure",
    "build_failure",
    "startup_failure",
    "assertion_failure",
    "lint_violation",
    "migration_failure",
    "runtime_error",
    "command_not_found",
    "permission_error",
    "timeout",
    "network_error",
    "configuration_error",
    "script_error",
    "unknown",
]

# ---------------------------------------------------------------------------
# Fault-side and confidence types (Phase 6)
# ---------------------------------------------------------------------------

FaultSideLiteral = Literal[
    "implementation",  # defect is in source/config files (not test files)
    "test",  # defect is in the test file itself (stale expectation, wrong assertion)
    "both",  # defects in both implementation and test files
    "environment",  # missing package, wrong runtime, system dependency issue
    "uncertain",  # cannot determine the responsible side from available evidence
]

DiagnosisConfidenceLiteral = Literal[
    "high",  # clear root cause; confident in fault_side
    "medium",  # reasonable hypothesis; some context missing or trace is ambiguous
    "low",  # insufficient evidence; best guess; fault_side may not be reliable
]

# ---------------------------------------------------------------------------
# Schemas (also used as LLM output schemas — compatible with OpenAI strict mode)
# ---------------------------------------------------------------------------


class DiagnosedError(BaseModel):
    """A single classified error extracted from command output."""

    error_class: ErrorClassLiteral
    affected_file: str | None = None
    line_number: int | None = None
    symbol: str | None = None
    error_message: str
    is_cascade: bool = False
    cascade_source_file: str | None = None


class ErrorDiagnosis(BaseModel):
    """
    Structured diagnosis produced by the error diagnostic tool when a command
    exits with an unexpected code.

    This is an LLM output schema. All fields are required in the JSON output.
    Nullable fields accept null. List fields accept empty arrays.

    Design contract:
    - Describes what went wrong and what needs to change.
    - Does NOT prescribe which subagent should perform the repair.
    - The orchestrator reads this to make routing decisions.
    """

    overall_error_class: ErrorClassLiteral
    # fault_side and confidence have defaults so that evidence payloads produced before
    # Phase 6 (which lack these keys) can still be validated without raising ValidationError.
    # The OpenAI strict schema util strips "default" values, so the LLM still emits both
    # fields explicitly — defaults only affect backward-compat model_validate() calls.
    fault_side: FaultSideLiteral = "uncertain"
    confidence: DiagnosisConfidenceLiteral = "medium"
    root_cause_errors: list[DiagnosedError] = Field(default_factory=list)
    cascade_errors: list[DiagnosedError] = Field(default_factory=list)
    cross_file_root_cause: str | None = None
    newly_discovered_files: list[str] = Field(default_factory=list)
    repair_directive: str
    is_recoverable: bool = True
