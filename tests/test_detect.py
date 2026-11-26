import pytest

from imagepipe.detect import detect
from tests.conftest import SAMPLES

EXPECTED = {
    "2020/holiday/IMG_0001.jpg": ("image", False),
    "2020/holiday/screenshot.png": ("image", False),
    "2020/raw/DSC_0002.NEF": ("raw", False),
    "2020/raw/IMG_0003.CR3": ("raw", False),
    "2021/phone/IMG_0004.HEIC": ("image", False),
    "2021/phone/IMG_0004.MOV": ("video", False),
    "2021/video/clip.mp4": ("video", False),
    "docs/scan.pdf": ("pdf", False),
    "docs/letter.docx": ("document", False),
    "docs/notes.txt": ("document", False),
    "archives/old.zip": ("archive", False),
    "broken/not_really.jpg": ("document", True),
    "broken/empty.jpg": ("empty", False),
    "junk/.DS_Store": ("junk", False),
    "junk/._IMG_0001.jpg": ("junk", False),
    "junk/Thumbs.db": ("junk", False),
    "2020/holiday/IMG_0001.xmp": ("sidecar", False),
    "mystery.bin": ("unknown", False),
}


@pytest.mark.parametrize("rel,expected", EXPECTED.items())
def test_detect_samples(rel, expected):
    data = SAMPLES[rel]()
    kind, mime, mismatch = detect(data[:4096], rel, len(data))
    assert (kind, mismatch) == expected, f"{rel}: got {kind} {mime} mismatch={mismatch}"


def test_tiff_without_raw_extension_is_image():
    assert detect(b"II*\x00" + b"\x00" * 20, "scan.tif", 24)[0] == "image"


def test_raw_named_jpg_is_mismatch():
    kind, _, mismatch = detect(b"\x00\x00\x00\x18ftypcrx " + b"\x00" * 20, "IMG.jpg", 40)
    assert kind == "raw" and mismatch


def test_avif():
    assert detect(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1", "a.avif", 40)[1] == "image/avif"


def test_png_named_jpg_is_mismatch():
    from tests.conftest import png_bytes
    data = png_bytes()
    kind, mime, mismatch = detect(data[:4096], "photo.jpg", len(data))
    assert (kind, mime, mismatch) == ("image", "image/png", True)
