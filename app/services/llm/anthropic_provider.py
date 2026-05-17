import json
import logging
import time
from typing import Any

import anthropic

from app.services.llm.base import LLMProvider
from app.services.llm.schema_utils import to_anthropic_tool_schema

logger = logging.getLogger("app.services.llm")


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_tokens: int = 32768,
    ) -> None:
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

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
            "llm_call_started provider=anthropic model=%s schema=%s timeout_s=%s prompt_chars_total=%d system_chars=%d user_chars=%d",
            self.model,
            schema_name,
            self.timeout,
            total_prompt_chars,
            system_chars,
            user_chars,
        )

        adapted_schema = to_anthropic_tool_schema(json_schema)
        response = None

        try:
            logger.info(
                "llm_http_request_started provider=anthropic model=%s schema=%s",
                self.model,
                schema_name,
            )

            _max_provider_retries = 5
            for _attempt in range(_max_provider_retries):
                try:
                    response = self.client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_prompt}],
                        tools=[
                            {
                                "name": schema_name,
                                "description": f"Respond with a valid {schema_name} object.",
                                "input_schema": adapted_schema,
                            }
                        ],
                        tool_choice={"type": "tool", "name": schema_name},
                    )
                    break
                except anthropic.RateLimitError as _exc:
                    if _attempt < _max_provider_retries - 1:
                        # Try to read retry-after from the response headers; fall
                        # back to an aggressive exponential schedule (30s, 60s, 120s…)
                        # because Anthropic rate-limit windows are typically ≥ 60 s.
                        _retry_after: float | None = None
                        _response_obj = getattr(_exc, "response", None)
                        if _response_obj is not None:
                            _header = getattr(_response_obj, "headers", {}).get("retry-after")
                            if _header is not None:
                                try:
                                    _retry_after = float(_header)
                                except (ValueError, TypeError):
                                    pass
                        _wait = _retry_after if _retry_after is not None else 30 * (2**_attempt)
                        logger.warning(
                            "llm_rate_limit_retry provider=anthropic model=%s schema=%s attempt=%s/%s wait_s=%s",
                            self.model,
                            schema_name,
                            _attempt + 1,
                            _max_provider_retries,
                            _wait,
                        )
                        time.sleep(_wait)
                        continue
                    raise
                except anthropic.APITimeoutError:
                    if _attempt < _max_provider_retries - 1:
                        _wait = 2 * (2**_attempt)
                        logger.warning(
                            "llm_timeout_retry provider=anthropic model=%s schema=%s attempt=%s/%s wait_s=%s",
                            self.model,
                            schema_name,
                            _attempt + 1,
                            _max_provider_retries,
                            _wait,
                        )
                        time.sleep(_wait)
                        continue
                    raise
                except anthropic.InternalServerError as _exc:
                    _status = getattr(_exc, "status_code", None)
                    if (
                        _status is not None
                        and _status >= 500
                        and _attempt < _max_provider_retries - 1
                    ):
                        _wait = 2 * (2**_attempt)
                        logger.warning(
                            "llm_http_5xx_retry provider=anthropic model=%s schema=%s status=%s attempt=%s/%s wait_s=%s",
                            self.model,
                            schema_name,
                            _status,
                            _attempt + 1,
                            _max_provider_retries,
                            _wait,
                        )
                        time.sleep(_wait)
                        continue
                    raise

            logger.info(
                "llm_http_request_finished provider=anthropic model=%s schema=%s",
                self.model,
                schema_name,
            )

            tool_block = next(
                (b for b in response.content if b.type == "tool_use" and b.name == schema_name),
                None,
            )
            if tool_block is None:
                raise ValueError(
                    f"Anthropic returned no tool_use block for schema '{schema_name}'. "
                    f"stop_reason={response.stop_reason}"
                )

            parsed: dict[str, Any] = tool_block.input
            output_chars = len(json.dumps(parsed))

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            input_tokens = getattr(response.usage, "input_tokens", None)
            output_tokens = getattr(response.usage, "output_tokens", None)
            total_tokens = (
                (input_tokens or 0) + (output_tokens or 0)
                if input_tokens is not None and output_tokens is not None
                else None
            )

            logger.info(
                "llm_call_completed provider=anthropic model=%s schema=%s duration_ms=%d prompt_chars_total=%d input_tokens=%s output_tokens=%s total_tokens=%s response_chars=%d",
                self.model,
                schema_name,
                elapsed_ms,
                total_prompt_chars,
                input_tokens,
                output_tokens,
                total_tokens,
                output_chars,
            )

            if elapsed_ms >= 10000:
                logger.warning(
                    "llm_call_slow provider=anthropic model=%s schema=%s duration_ms=%d prompt_chars_total=%d input_tokens=%s output_tokens=%s total_tokens=%s",
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
            input_tokens = (
                getattr(response.usage, "input_tokens", None) if response is not None else None
            )
            output_tokens = (
                getattr(response.usage, "output_tokens", None) if response is not None else None
            )
            total_tokens = (
                (input_tokens or 0) + (output_tokens or 0)
                if input_tokens is not None and output_tokens is not None
                else None
            )

            logger.exception(
                "llm_call_failed provider=anthropic model=%s schema=%s duration_ms=%d prompt_chars_total=%d input_tokens=%s output_tokens=%s total_tokens=%s error=%s",
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
