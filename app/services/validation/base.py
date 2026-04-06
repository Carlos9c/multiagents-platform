from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.validation.contracts import TaskValidationInput, ValidationResult


class BaseTaskValidator(ABC):
    validator_key: str
    producer_key: str

    @abstractmethod
    def validate(self, validation_input: TaskValidationInput) -> ValidationResult:
        raise NotImplementedError