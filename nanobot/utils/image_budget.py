"""Safe, bounded image preparation for model vision inputs."""

from __future__ import annotations

import io
import warnings
from typing import Final

from PIL import Image, ImageOps

MAX_IMAGE_PIXELS: Final = 4_000_000
# Hard decode ceiling: permits bounded downsampling while rejecting huge bombs.
MAX_DECODE_PIXELS: Final = 8_000_000
MAX_IMAGE_BYTES: Final = 80_000
MAX_IMAGE_INPUT_BYTES: Final = 32_000_000
MAX_IMAGE_DIMENSION: Final = 10_000
_MIN_JPEG_QUALITY: Final = 45


def _to_rgb(image: Image.Image) -> Image.Image:
    """Flatten alpha onto white; JPEG has no transparency."""
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def prepare_image(raw: bytes, mime: str | None = None) -> tuple[bytes, str] | None:
    """Validate and bound an image for model vision input.

    Header dimensions are checked before any operation that decodes pixels.
    Animated images are rejected rather than silently sending an arbitrary frame.
    """
    if not raw or len(raw) > MAX_IMAGE_INPUT_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Opening reads headers only. Do this before verify/transpose/convert.
            with Image.open(io.BytesIO(raw)) as source:
                width, height = source.size
                if (width < 1 or height < 1 or width > MAX_IMAGE_DIMENSION
                        or height > MAX_IMAGE_DIMENSION
                        or width * height > MAX_DECODE_PIXELS):
                    return None
                if getattr(source, "n_frames", 1) > 1:
                    return None
            # Verify only after the cheap dimension and frame checks.
            with Image.open(io.BytesIO(raw)) as source:
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                if len(raw) <= MAX_IMAGE_BYTES and width * height <= MAX_IMAGE_PIXELS:
                    detected = Image.MIME.get(source.format, "image/png").lower()
                    return raw, detected
                image = ImageOps.exif_transpose(source)
                image = _to_rgb(image)
                scale = min(1.0, (MAX_IMAGE_PIXELS / (image.width * image.height)) ** 0.5)
                if scale < 1:
                    image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
                for _ in range(5):
                    for quality in (85, 70, 55, _MIN_JPEG_QUALITY):
                        out = io.BytesIO()
                        image.save(out, format="JPEG", quality=quality, optimize=True)
                        if out.tell() <= MAX_IMAGE_BYTES:
                            return out.getvalue(), "image/jpeg"
                    if image.width <= 1 or image.height <= 1:
                        break
                    image = image.resize((max(1, int(image.width * 0.75)), max(1, int(image.height * 0.75))), Image.Resampling.LANCZOS)
                return None
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None
