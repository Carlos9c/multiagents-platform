from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

from app.services.analysis.analyzers.image_analyzer import ImageFileAnalyzer, _fallback_analysis
from app.services.analysis.base import FileInput


def _make_png_file(tmp_path: Path, name: str = "icon.png") -> FileInput:
    try:
        from PIL import Image

        img = Image.new("RGB", (64, 64), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
    except ImportError:
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200

    path = tmp_path / name
    path.write_bytes(data)
    return FileInput(path=name, absolute_path=path)


def _make_llm_response() -> dict:
    return {
        "use_case": "application icon",
        "visual_style": "flat icon",
        "primary_colors": ["blue #2563EB", "white"],
        "dimensions_description": "square icon for small display sizes",
        "summary": "Application icon — flat style, blue on white, optimized for small sizes.",
    }


# ── Happy path ───────────────────────────────────────────────────────────────


def test_analyze_produces_file_analysis(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.return_value = _make_llm_response()

    fi = _make_png_file(tmp_path)
    analyzer = ImageFileAnalyzer(llm_provider=llm)
    results = analyzer.analyze_files([fi])

    assert len(results) == 1
    fa = results[0]
    assert fa.path == "icon.png"
    assert fa.file_type == "asset"
    assert fa.language is None
    assert fa.summary  # summary is non-empty
    assert "application icon" in fa.key_elements
    assert "flat icon" in fa.key_elements


def test_analyze_passes_image_bytes_to_llm(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.return_value = _make_llm_response()

    fi = _make_png_file(tmp_path)
    analyzer = ImageFileAnalyzer(llm_provider=llm)
    analyzer.analyze_files([fi])

    call_kwargs = llm.generate_structured.call_args[1]
    assert "images" in call_kwargs
    assert isinstance(call_kwargs["images"], list)
    assert len(call_kwargs["images"]) == 1
    assert isinstance(call_kwargs["images"][0], bytes)


def test_analyze_multiple_files(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.return_value = _make_llm_response()

    files = [_make_png_file(tmp_path, f"img{i}.png") for i in range(3)]
    analyzer = ImageFileAnalyzer(llm_provider=llm)
    results = analyzer.analyze_files(files)

    assert len(results) == 3
    assert llm.generate_structured.call_count == 3


# ── Fallback on LLM error ────────────────────────────────────────────────────


def test_analyze_returns_fallback_on_llm_failure(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = RuntimeError("LLM down")

    fi = _make_png_file(tmp_path)
    analyzer = ImageFileAnalyzer(llm_provider=llm)
    results = analyzer.analyze_files([fi])

    assert len(results) == 1
    assert results[0].file_type == "asset"
    assert "unavailable" in results[0].summary.lower()


# ── Fallback on missing file ──────────────────────────────────────────────────


def test_analyze_returns_fallback_on_read_error(tmp_path: Path):
    llm = MagicMock()
    fi = FileInput(path="missing.png", absolute_path=tmp_path / "does_not_exist.png")

    analyzer = ImageFileAnalyzer(llm_provider=llm)
    results = analyzer.analyze_files([fi])

    assert len(results) == 1
    assert results[0].file_type == "asset"
    llm.generate_structured.assert_not_called()


# ── Supported extensions ─────────────────────────────────────────────────────


def test_supported_extensions_include_common_formats():
    assert ".png" in ImageFileAnalyzer.supported_extensions
    assert ".jpg" in ImageFileAnalyzer.supported_extensions
    assert ".webp" in ImageFileAnalyzer.supported_extensions
    assert ".py" not in ImageFileAnalyzer.supported_extensions


# ── _fallback_analysis ────────────────────────────────────────────────────────


def test_fallback_analysis_uses_extension():
    fa = _fallback_analysis("assets/logo.webp")
    assert "WEBP" in fa.summary
    assert fa.file_type == "asset"
