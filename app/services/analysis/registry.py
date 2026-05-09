from __future__ import annotations

from pathlib import Path

from app.services.analysis.base import BaseFileAnalyzer, FileInput


class FileAnalyzerRegistry:
    """
    Deterministic, extension-based router.

    Iterates registered analyzers in order and returns the first one whose
    `supported_extensions` contains the file's suffix.  Files whose extension
    matches nothing fall through to `fallback`.
    """

    def __init__(
        self,
        analyzers: list[BaseFileAnalyzer],
        fallback: BaseFileAnalyzer,
    ) -> None:
        self._analyzers = analyzers
        self._fallback = fallback

    def get_analyzer(self, path: str) -> BaseFileAnalyzer:
        ext = Path(path).suffix.lower()
        for analyzer in self._analyzers:
            if ext in analyzer.supported_extensions:
                return analyzer
        return self._fallback

    def group_by_analyzer(
        self, files: list[FileInput]
    ) -> list[tuple[BaseFileAnalyzer, list[FileInput]]]:
        """
        Preserve declaration order of analyzers so results are predictable.
        Files that share an analyzer are batched together.
        """
        order: list[BaseFileAnalyzer] = []
        groups: dict[int, list[FileInput]] = {}

        for file in files:
            analyzer = self.get_analyzer(file.path)
            key = id(analyzer)
            if key not in groups:
                order.append(analyzer)
                groups[key] = []
            groups[key].append(file)

        return [(analyzer, groups[id(analyzer)]) for analyzer in order]
