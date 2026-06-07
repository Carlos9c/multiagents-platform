from __future__ import annotations

import io
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from PIL.Image import Image

ResizeMode = Literal["fit", "fill", "crop"]


def resize_image(
    *,
    source_bytes: bytes,
    width: int,
    height: int,
    mode: ResizeMode = "fit",
    output_format: str = "png",
) -> bytes:
    """
    Resize image bytes to the target dimensions and return the result as bytes.

    Modes:
    - fit:  Maintains aspect ratio. The image fits entirely within the target
            box. Any remaining space is filled with white (RGBA: transparent).
    - fill: Covers the entire target area. The image is scaled so the shorter
            side fills the target, then cropped from the center.
    - crop: Crops the image from the center to the exact target dimensions
            without any scaling.

    ``output_format`` controls the output container: "png", "jpg"/"jpeg", "webp".
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for image resizing. Install it with: pip install Pillow"
        ) from exc

    fmt = output_format.upper()
    if fmt == "JPG":
        fmt = "JPEG"

    with Image.open(io.BytesIO(source_bytes)) as img:
        # Ensure RGBA for transparent padding in fit mode; convert to RGB for JPEG
        if mode == "fit":
            resized = _fit(img, width, height)
        elif mode == "fill":
            resized = _fill(img, width, height)
        else:
            resized = _crop(img, width, height)

        if fmt == "JPEG" and resized.mode in ("RGBA", "P"):
            background = Image.new("RGB", resized.size, (255, 255, 255))
            background.paste(resized, mask=resized.split()[3] if resized.mode == "RGBA" else None)
            resized = background

        buf = io.BytesIO()
        resized.save(buf, format=fmt)
        return buf.getvalue()


def _fit(img: Image, width: int, height: int) -> Image:
    from PIL import Image

    img_copy = img.copy()
    img_copy.thumbnail((width, height), Image.LANCZOS)

    # Center on a canvas of the exact target size
    canvas_mode = "RGBA" if img_copy.mode == "RGBA" else "RGB"
    bg_color: tuple = (0, 0, 0, 0) if canvas_mode == "RGBA" else (255, 255, 255)
    canvas = Image.new(canvas_mode, (width, height), bg_color)
    offset_x = (width - img_copy.width) // 2
    offset_y = (height - img_copy.height) // 2
    if img_copy.mode == "RGBA":
        canvas.paste(img_copy, (offset_x, offset_y), img_copy)
    else:
        canvas.paste(img_copy, (offset_x, offset_y))
    return canvas


def _fill(img: Image, width: int, height: int) -> Image:
    from PIL import Image

    src_ratio = img.width / img.height
    tgt_ratio = width / height

    if src_ratio > tgt_ratio:
        # Image is wider than target: scale by height
        new_h = height
        new_w = int(img.width * height / img.height)
    else:
        # Image is taller than target: scale by width
        new_w = width
        new_h = int(img.height * width / img.width)

    scaled = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def _crop(img: Image, width: int, height: int) -> Image:
    left = max(0, (img.width - width) // 2)
    top = max(0, (img.height - height) // 2)
    right = left + min(width, img.width)
    bottom = top + min(height, img.height)
    return img.crop((left, top, right, bottom))
