import json
import logging
import time
from typing import Any

from openai import OpenAI

from app.services.llm.base import LLMProvider


logger = logging.getLogger("app.services.llm")


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_retries: int = 1,
    ) -> None:
        self.client = OpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    @staticmethod
    def _safe_usage_value(response: Any, field_name: str) -> int | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        value = getattr(usage, field_name, None)
        if isinstance(value, int):
            return value

        if isinstance(usage, dict):
            raw = usage.get(field_name)
            if isinstance(raw, int):
                return raw

        return None

    @staticmethod
    def _truncate_for_log(value: str | None, limit: int = 500) -> str | None:
        if not value:
            return None
        if len(value) <= limit:
            return value
        return value[:limit] + "...(truncated)"

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        system_chars = len(system_prompt or "")
        user_chars = len(user_prompt or "")
        total_prompt_chars = system_chars + user_chars

        logger.info(
            "llm_call_started provider=openai model=%s schema=%s timeout_s=%s max_retries=%s prompt_chars_total=%d system_chars=%d user_chars=%d",
            self.model,
            schema_name,
            self.timeout,
            self.max_retries,
            total_prompt_chars,
            system_chars,
            user_chars,
        )

        response = None

        try:
            logger.info(
                "llm_http_request_started provider=openai model=%s schema=%s",
                self.model,
                schema_name,
            )

            response = self.client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": system_prompt}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}],
                    },
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": json_schema,
                        "strict": True,
                    }
                },
            )

            logger.info(
                "llm_http_request_finished provider=openai model=%s schema=%s",
                self.model,
                schema_name,
            )

            output_text = getattr(response, "output_text", None)
            if not output_text:
                raise ValueError("OpenAI returned an empty structured response.")

            logger.info(
                "llm_response_text_received provider=openai model=%s schema=%s response_chars=%d",
                self.model,
                schema_name,
                len(output_text),
            )

            parsed = json.loads(output_text)

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            input_tokens = self._safe_usage_value(response, "input_tokens")
            output_tokens = self._safe_usage_value(response, "output_tokens")
            total_tokens = self._safe_usage_value(response, "total_tokens")

            logger.info(
                "llm_call_completed provider=openai model=%s schema=%s duration_ms=%d prompt_chars_total=%d input_tokens=%s output_tokens=%s total_tokens=%s response_chars=%d",
                self.model,
                schema_name,
                elapsed_ms,
                total_prompt_chars,
                input_tokens,
                output_tokens,
                total_tokens,
                len(output_text),
            )

            if elapsed_ms >= 10000:
                logger.warning(
                    "llm_call_slow provider=openai model=%s schema=%s duration_ms=%d prompt_chars_total=%d input_tokens=%s output_tokens=%s total_tokens=%s",
                    self.model,
                    schema_name,
                    elapsed_ms,
                    total_prompt_chars,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                )

            return parsed

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)

            input_tokens = self._safe_usage_value(response, "input_tokens") if response is not None else None
            output_tokens = self._safe_usage_value(response, "output_tokens") if response is not None else None
            total_tokens = self._safe_usage_value(response, "total_tokens") if response is not None else None

            logger.exception(
                "llm_call_failed provider=openai model=%s schema=%s duration_ms=%d prompt_chars_total=%d input_tokens=%s output_tokens=%s total_tokens=%s error=%s",
                self.model,
                schema_name,
                elapsed_ms,
                total_prompt_chars,
                input_tokens,
                output_tokens,
                total_tokens,
                self._truncate_for_log(str(exc)),
            )
            raise