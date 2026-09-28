"""Safe, bounded image preparation for model vision inputs."""

from __future__ import annotations

import io
import warnings
from typing import Final

from PIL import Image, ImageOps

MAX_IMAGE_PIXELS: Final = 4_000_000
MAX_IMAGE_BYTES: Final = 80_000
_MIN_JPEG_QUALITY: Final = 45


def prepare_image(raw: bytes, mime: str | None = None) -> tuple[bytes, str] | None:
    """Validate and bound an image for model vision input."""
    if not raw or len(raw) > 32_000_000:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                if source.width < 1 or source.height < 1:
                    return None
                if source.width * source.height > MAX_IMAGE_PIXELS or len(raw) > MAX_IMAGE_BYTES:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    scale = min(1.0, (MAX_IMAGE_PIXELS / (image.width * image.height)) ** 0.5)
                    if scale < 1:
                        image = image.resize(
                            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                            Image.Resampling.LANCZOS,
                        )
                    for _ in range(5):
                        for quality in (85, 70, 55, _MIN_JPEG_QUALITY):
                            out = io.BytesIO()
                            image.save(out, format="JPEG", quality=quality, optimize=True)
                            if out.tell() <= MAX_IMAGE_BYTES:
                                return out.getvalue(), "image/jpeg"
                        if image.width <= 1 or image.height <= 1:
                            break
                        image = image.resize(
                            (max(1, int(image.width * 0.75)), max(1, int(image.height * 0.75))),
                            Image.Resampling.LANCZOS,
                        )
                    return None
                detected = Image.MIME.get(source.format, "image/png").lower()
                return raw, detected
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None
