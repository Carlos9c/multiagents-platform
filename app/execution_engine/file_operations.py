from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


FILE_OPERATION_CREATE = "create"
FILE_OPERATION_MODIFY = "modify"

VALID_FILE_OPERATIONS = {
    FILE_OPERATION_CREATE,
    FILE_OPERATION_MODIFY,
}

FILE_CATEGORY_SOURCE = "source"
FILE_CATEGORY_TEST = "test"
FILE_CATEGORY_CONFIG = "config"
FILE_CATEGORY_INTEGRATION = "integration"
FILE_CATEGORY_DOCS = "docs"

VALID_FILE_CATEGORIES = {
    FILE_CATEGORY_SOURCE,
    FILE_CATEGORY_TEST,
    FILE_CATEGORY_CONFIG,
    FILE_CATEGORY_INTEGRATION,
    FILE_CATEGORY_DOCS,
}

EDIT_MODE_FULL_REPLACE = "full_replace"
EDIT_MODE_TARGETED_UPDATE = "targeted_update"
EDIT_MODE_APPEND = "append"

VALID_EDIT_MODES = {
    EDIT_MODE_FULL_REPLACE,
    EDIT_MODE_TARGETED_UPDATE,
    EDIT_MODE_APPEND,
}


class FileOperation(BaseModel):
    operation: Literal["create", "modify"]
    path: str
    reason: str
    purpose: str

    category: Literal["source", "test", "config", "integration", "docs"] = "source"
    sequence: int = 1
    depends_on_paths: list[str] = Field(default_factory=list)

    integration_notes: list[str] = Field(default_factory=list)
    edit_mode: Literal["full_replace", "targeted_update", "append"] | None = None
    symbols_expected: list[str] = Field(default_factory=list)


class FileOperationPlan(BaseModel):
    summary: str
    operations: list[FileOperation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    rejection_reason: str | None = None
    remaining_scope: str | None = None
    blockers_found: list[str] = Field(default_factory=list)

    def sorted_operations(self) -> list[FileOperation]:
        return sorted(
            self.operations,
            key=lambda item: (
                item.sequence,
                item.category,
                item.path,
            ),
        )


class MaterializedFile(BaseModel):
    path: str
    operation: Literal["create", "modify"]
    content: str
    rationale: str


class FileMaterializationResult(BaseModel):
    summary: str
    files: list[MaterializedFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
