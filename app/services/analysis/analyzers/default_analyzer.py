from __future__ import annotations

from pathlib import Path

from app.services.analysis.base import BaseFileAnalyzer, FileAnalysis, FileInput


class DefaultFileAnalyzer(BaseFileAnalyzer):
    """
    Fallback analyzer for files whose extension is not claimed by any
    registered analyzer (typically binary assets: images, archives, compiled
    artefacts, etc.).

    No LLM call is made.  The entry records basic metadata derivable from
    the path alone so the file appears in the analysis dictionary without
    breaking the accumulation loop.
    """

    # Empty: this analyzer is never selected by extension — it is the fallback.
    supported_extensions: frozenset[str] = frozenset()

    def analyze_files(self, files: list[FileInput]) -> list[FileAnalysis]:
        return [self._minimal_entry(fi) for fi in files]

    def _minimal_entry(self, fi: FileInput) -> FileAnalysis:
        ext = Path(fi.path).suffix.lower() or "no extension"
        return FileAnalysis(
            path=fi.path,
            file_type="asset",
            language=None,
            summary=f"Unrecognised or binary file ({ext}).",
            key_elements=[],
            dependencies=[],
        )
