from __future__ import annotations

from app.services.validation.base import BaseTaskValidator
from app.services.validation.validators.code_change_agent_validator import (
    CodeChangeAgentValidator,
)
from app.services.validation.validators.command_runner_agent_validator import (
    CommandRunnerAgentValidator,
)


class ValidationRegistryError(Exception):
    """Raised when a validator cannot be resolved from the registry."""


class ValidationRegistry:
    def __init__(self, validators: list[BaseTaskValidator] | None = None) -> None:
        resolved_validators = validators or [
            CodeChangeAgentValidator(),
            CommandRunnerAgentValidator(),
        ]

        self._validators_by_key: dict[str, BaseTaskValidator] = {}
        self._validators_by_producer: dict[str, BaseTaskValidator] = {}

        for validator in resolved_validators:
            if validator.validator_key in self._validators_by_key:
                raise ValidationRegistryError(
                    f"Duplicate validator_key registered: {validator.validator_key}"
                )

            if validator.producer_key in self._validators_by_producer:
                raise ValidationRegistryError(
                    f"Duplicate producer_key registered: {validator.producer_key}"
                )

            self._validators_by_key[validator.validator_key] = validator
            self._validators_by_producer[validator.producer_key] = validator

    def get_by_key(self, validator_key: str) -> BaseTaskValidator:
        validator = self._validators_by_key.get(validator_key)
        if validator is None:
            raise ValidationRegistryError(
                f"No validator registered for validator_key='{validator_key}'"
            )
        return validator

    def get_by_producer(self, producer_key: str) -> BaseTaskValidator:
        validator = self._validators_by_producer.get(producer_key)
        if validator is None:
            raise ValidationRegistryError(
                f"No validator registered for producer_key='{producer_key}'"
            )
        return validator

    def has_producer(self, producer_key: str) -> bool:
        return producer_key in self._validators_by_producer

    def all_validator_keys(self) -> list[str]:
        return list(self._validators_by_key.keys())

    def all_producer_keys(self) -> list[str]:
        return list(self._validators_by_producer.keys())
