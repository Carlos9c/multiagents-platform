from typing import Literal

# Single canonical definition of valid task types for the entire system.
# All schemas, services, and validators must import from here.
#
# Planner-assigned types:
#   requirements, design, planning, implementation, testing,
#   review, documentation, onboarding
# Execution/recovery types (added to match system operations):
#   configuration, refactor
TaskType = Literal[
    "requirements",
    "design",
    "planning",
    "implementation",
    "testing",
    "review",
    "documentation",
    "onboarding",
    "configuration",
    "refactor",
]
