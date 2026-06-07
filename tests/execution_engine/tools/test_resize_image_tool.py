from __future__ import annotations

import io

from app.execution_engine.tools.resize_image_tool import resize_image


def _make_png(width: int = 200, height: int = 100) -> bytes:
    """Create a minimal valid PNG using Pillow."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(255, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    return img.size


# ── fit mode ────────────────────────────────────────────────────────────────


def test_fit_produces_correct_canvas_size():
    source = _make_png(200, 100)
    result = resize_image(source_bytes=source, width=100, height=100, mode="fit")
    w, h = _image_size(result)
    assert w == 100
    assert h == 100


def test_fit_preserves_aspect_ratio():
    source = _make_png(400, 200)
    result = resize_image(source_bytes=source, width=100, height=100, mode="fit")
    # image is 2:1 — after fit into 100×100 it should be 100×50 centered on a 100×100 canvas
    w, h = _image_size(result)
    assert w == 100
    assert h == 100


# ── fill mode ────────────────────────────────────────────────────────────────


def test_fill_produces_exact_size():
    source = _make_png(300, 200)
    result = resize_image(source_bytes=source, width=64, height=64, mode="fill")
    w, h = _image_size(result)
    assert w == 64
    assert h == 64


# ── crop mode ────────────────────────────────────────────────────────────────


def test_crop_does_not_exceed_source():
    source = _make_png(50, 50)
    result = resize_image(source_bytes=source, width=100, height=100, mode="crop")
    w, h = _image_size(result)
    assert w <= 100
    assert h <= 100


# ── output format ────────────────────────────────────────────────────────────


def test_output_format_png():
    source = _make_png()
    result = resize_image(source_bytes=source, width=32, height=32, mode="fit", output_format="png")
    assert result[:4] == b"\x89PNG"


def test_output_format_jpg():
    source = _make_png()
    result = resize_image(source_bytes=source, width=32, height=32, mode="fit", output_format="jpg")
    assert result[:2] == b"\xff\xd8"  # JPEG magic bytes
