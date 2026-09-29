from io import BytesIO

from PIL import Image

from nanobot.utils.image_budget import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, prepare_image


def _image(size=(3000, 2000), fmt="PNG"):
    out = BytesIO()
    Image.new("RGB", size, (20, 80, 140)).save(out, format=fmt)
    return out.getvalue()


def test_large_image_is_resized_and_bounded():
    raw, mime = prepare_image(_image())
    assert mime == "image/jpeg"
    assert len(raw) <= MAX_IMAGE_BYTES
    with Image.open(BytesIO(raw)) as image:
        assert image.width * image.height <= MAX_IMAGE_PIXELS


def test_invalid_image_is_rejected():
    assert prepare_image(b"not an image", "image/png") is None


def test_small_image_is_validated():
    raw = _image((20, 20))
    prepared = prepare_image(raw, "image/png")
    assert prepared == (raw, "image/png")


def test_huge_dimensions_rejected_from_header():
    import struct
    import zlib

    ihdr = struct.pack("!IIBBBBB", 8000, 8000, 8, 2, 0, 0, 0)
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + b"IHDR" + ihdr
    raw += struct.pack("!I", zlib.crc32(b"IHDR" + ihdr) & 0xffffffff)
    assert prepare_image(raw, "image/png") is None
