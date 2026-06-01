"""Tests for the OpenAIProvider JSON extra-data fallback.

Covers the case where the model appends trailing text after the JSON object
(observed with gpt-5.4-mini under structured-output mode).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.llm.openai_provider import OpenAIProvider


def _make_provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="test-key", model="gpt-test")


def _make_response(output_text: str) -> MagicMock:
    response = MagicMock()
    response.output_text = output_text
    response.usage = None
    return response


@pytest.fixture()
def provider() -> OpenAIProvider:
    return _make_provider()


def _call_with_output(provider: OpenAIProvider, output_text: str) -> dict:
    """Patch the OpenAI client and call generate_structured with the given output_text."""
    schema = {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
    mock_response = _make_response(output_text)

    with patch.object(provider.client.responses, "create", return_value=mock_response):
        return provider.generate_structured(
            system_prompt="sys",
            user_prompt="usr",
            schema_name="test_schema",
            json_schema=schema,
        )


class TestCleanResponse:
    def test_parses_clean_json(self, provider: OpenAIProvider) -> None:
        result = _call_with_output(provider, '{"key": "value"}')
        assert result == {"key": "value"}

    def test_parses_json_with_leading_and_trailing_whitespace(
        self, provider: OpenAIProvider
    ) -> None:
        result = _call_with_output(provider, '  \n{"key": "value"}\n  ')
        assert result == {"key": "value"}


class TestExtraDataFallback:
    def test_parses_json_with_trailing_text(self, provider: OpenAIProvider) -> None:
        output = '{"key": "value"}\nSome trailing explanation from the model.'
        result = _call_with_output(provider, output)
        assert result == {"key": "value"}

    def test_parses_json_with_trailing_newline_and_text(self, provider: OpenAIProvider) -> None:
        output = '{"key": "hello"}\n\nExtra content here.'
        result = _call_with_output(provider, output)
        assert result == {"key": "hello"}

    def test_logs_warning_on_fallback(self, provider: OpenAIProvider) -> None:
        output = '{"key": "x"} trailing stuff'
        with patch("app.services.llm.openai_provider.logger") as mock_logger:
            _call_with_output(provider, output)
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("llm_json_extra_data_fallback" in c for c in warning_calls)

    def test_does_not_log_warning_on_clean_json(self, provider: OpenAIProvider) -> None:
        output = '{"key": "clean"}'
        with patch("app.services.llm.openai_provider.logger") as mock_logger:
            _call_with_output(provider, output)
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert not any("llm_json_extra_data_fallback" in c for c in warning_calls)

    def test_raises_on_completely_invalid_json(self, provider: OpenAIProvider) -> None:
        output = "not json at all"
        with pytest.raises(json.JSONDecodeError):
            _call_with_output(provider, output)

    def test_raises_on_empty_response(self, provider: OpenAIProvider) -> None:
        with pytest.raises(ValueError, match="empty structured response"):
            _call_with_output(provider, "")
