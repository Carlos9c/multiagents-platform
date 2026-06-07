from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.services.analysis.base import BaseFileAnalyzer, FileAnalysis, FileInput
from app.services.llm.base import LLMProvider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico"}
)

_SYSTEM_PROMPT = prompt_loader.get("image_analyzer")


class _ImageDescriptionOutput(BaseModel):
    use_case: str
    visual_style: str
    primary_colors: list[str]
    dimensions_description: str
    summary: str


def _build_user_prompt(image_path: str) -> str:
    from app.services.prompt_loader import prompt_loader as _pl

    _pl.validate_builder_inputs(
        "image_analyzer",
        "main",
        {"image_path": image_path},
    )
    return f"Image path: {image_path}\n\nPlease analyze the image shown above."


class ImageFileAnalyzer(BaseFileAnalyzer):
    """
    Analyzes image files using a vision-capable LLM to produce FileAnalysis entries
    for the codebase catalog.

    Each image is analyzed individually — no batching — because images cannot be
    meaningfully combined into a single text batch.
    """

    supported_extensions = IMAGE_EXTENSIONS

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    def analyze_files(self, files: list[FileInput]) -> list[FileAnalysis]:
        results: list[FileAnalysis] = []
        for fi in files:
            results.append(self._analyze_single(fi))
        return results

    def _analyze_single(self, fi: FileInput) -> FileAnalysis:
        try:
            image_bytes = fi.absolute_path.read_bytes()
        except OSError as exc:
            logger.warning("image_analyzer_read_failed path=%s error=%s", fi.path, str(exc))
            return _fallback_analysis(fi.path)

        user_prompt = _build_user_prompt(fi.path)

        try:
            raw: dict[str, Any] = self._llm.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="image_description_output",
                json_schema=_ImageDescriptionOutput.model_json_schema(),
                images=[image_bytes],
            )
            output = _ImageDescriptionOutput.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "image_analyzer_llm_failed path=%s error=%s",
                fi.path,
                str(exc),
            )
            return _fallback_analysis(fi.path)

        return FileAnalysis(
            path=fi.path,
            file_type="asset",
            language=None,
            summary=output.summary,
            key_elements=[output.use_case, output.visual_style],
            dependencies=[],
        )


def _fallback_analysis(path: str) -> FileAnalysis:
    ext = path.rsplit(".", 1)[-1].upper() if "." in path else "IMAGE"
    return FileAnalysis(
        path=path,
        file_type="asset",
        language=None,
        summary=f"Binary image asset ({ext}). Visual analysis unavailable.",
        key_elements=[],
        dependencies=[],
    )
