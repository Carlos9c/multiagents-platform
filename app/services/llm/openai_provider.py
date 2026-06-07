import base64
import json
import logging
import time
from typing import Any

from openai import APITimeoutError, InternalServerError, OpenAI, RateLimitError

from app.services.llm.base import LLMProvider
from app.services.llm.schema_utils import to_openai_strict_json_schema

logger = logging.getLogger("app.services.llm")


def _detect_image_mime_type(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    return "image/png"


def _build_image_content_block(image_bytes: bytes) -> dict[str, Any]:
    mime = _detect_image_mime_type(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:{mime};base64,{b64}",
    }


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
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        system_chars = len(system_prompt or "")
        user_chars = len(user_prompt or "")
        total_prompt_chars = system_chars + user_chars
        image_count = len(images) if images else 0

        logger.info(
            "llm_call_started provider=openai model=%s schema=%s timeout_s=%s max_retries=%s prompt_chars_total=%d system_chars=%d user_chars=%d images=%d",
            self.model,
            schema_name,
            self.timeout,
            self.max_retries,
            total_prompt_chars,
            system_chars,
            user_chars,
            image_count,
        )

        adapted_schema = to_openai_strict_json_schema(json_schema)
        response = None

        user_content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
        if images:
            for img_bytes in images:
                user_content.append(_build_image_content_block(img_bytes))

        try:
            logger.info(
                "llm_http_request_started provider=openai model=%s schema=%s",
                self.model,
                schema_name,
            )

            _max_provider_retries = 5
            for _attempt in range(_max_provider_retries):
                try:
                    response = self.client.responses.create(
                        model=self.model,
                        input=[
                            {
                                "role": "system",
                                "content": [{"type": "input_text", "text": system_prompt}],
                            },
                            {
                                "role": "user",
                                "content": user_content,
                            },
                        ],
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": schema_name,
                                "schema": adapted_schema,
                                "strict": True,
                            }
                        },
                    )
                    break
                except RateLimitError as _exc:
                    if _attempt < _max_provider_retries - 1:
                        # Honour the retry-after header when present; otherwise use
                        # an aggressive exponential schedule (30 s, 60 s, 120 s…).
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
                            "llm_rate_limit_retry provider=openai model=%s schema=%s attempt=%s/%s wait_s=%s",
                            self.model,
                            schema_name,
                            _attempt + 1,
                            _max_provider_retries,
                            _wait,
                        )
                        time.sleep(_wait)
                        continue
                    raise
                except APITimeoutError:
                    if _attempt < _max_provider_retries - 1:
                        _wait = 2 * (2**_attempt)  # 2, 4, 8, 16 s
                        logger.warning(
                            "llm_timeout_retry provider=openai model=%s schema=%s attempt=%s/%s wait_s=%s",
                            self.model,
                            schema_name,
                            _attempt + 1,
                            _max_provider_retries,
                            _wait,
                        )
                        time.sleep(_wait)
                        continue
                    raise
                except InternalServerError as _exc:
                    _status = getattr(_exc, "status_code", None)
                    if (
                        _status is not None
                        and _status >= 500
                        and _attempt < _max_provider_retries - 1
                    ):
                        _wait = 2 * (2**_attempt)  # 2, 4, 8, 16 s
                        logger.warning(
                            "llm_http_5xx_retry provider=openai model=%s schema=%s status=%s attempt=%s/%s wait_s=%s",
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

            output_text_stripped = output_text.strip()
            try:
                parsed = json.loads(output_text_stripped)
            except json.JSONDecodeError:
                # Some models append trailing text after the JSON object even
                # with structured-output mode enabled. raw_decode parses the
                # first complete JSON value and ignores anything that follows.
                logger.warning(
                    "llm_json_extra_data_fallback provider=openai model=%s schema=%s — using raw_decode",
                    self.model,
                    schema_name,
                )
                decoder = json.JSONDecoder()
                parsed, _ = decoder.raw_decode(output_text_stripped)

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

            input_tokens = (
                self._safe_usage_value(response, "input_tokens") if response is not None else None
            )
            output_tokens = (
                self._safe_usage_value(response, "output_tokens") if response is not None else None
            )
            total_tokens = (
                self._safe_usage_value(response, "total_tokens") if response is not None else None
            )

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
