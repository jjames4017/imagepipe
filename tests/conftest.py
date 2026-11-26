"""Shared fixtures. Every test file is generated here - no personal photos."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from imagepipe.config import load_config


def jpeg_with_exif(make="TestCam", model="Model X", date="2020:01:02 03:04:05", size=(64, 48)) -> bytes:
    im = Image.new("RGB", size, (200, 100, 50))
    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = model
    exif.get_ifd(0x8769)[0x9003] = date
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def png_bytes(size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (0, 128, 255)).save(buf, "PNG")
    return buf.getvalue()


def zip_bytes(members: dict[str, bytes], ext_name="x.zip") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


SAMPLES = {
    "2020/holiday/IMG_0001.jpg": lambda: jpeg_with_exif(),
    "2020/holiday/IMG_0001 copy.jpg": lambda: jpeg_with_exif(),          # exact duplicate
    "2020/holiday/screenshot.png": lambda: png_bytes(),
    "2020/raw/DSC_0002.NEF": lambda: b"II*\x00" + b"\x00" * 200,
    "2020/raw/IMG_0003.CR3": lambda: b"\x00\x00\x00\x18ftypcrx \x00\x00\x00\x01crx isom" + b"\x00" * 100,
    "2021/phone/IMG_0004.HEIC": lambda: b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 100,
    "2021/phone/IMG_0004.MOV": lambda: b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 100,
    "2021/video/clip.mp4": lambda: b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 100,
    "docs/scan.pdf": lambda: b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 100,
    "docs/letter.docx": lambda: zip_bytes({"word/document.xml": b"<w/>"}),
    "docs/notes.txt": lambda: b"just some notes\n",
    "archives/old.zip": lambda: zip_bytes({"a/photo1.jpg": b"\xff\xd8\xff" + b"0" * 50, "a/readme.txt": b"hi"}),
    "broken/not_really.jpg": lambda: b"<!DOCTYPE html><html><body>404</body></html>",
    "broken/empty.jpg": lambda: b"",
    "junk/.DS_Store": lambda: b"\x00\x00\x00\x01Bud1" + b"\x00" * 50,
    "junk/._IMG_0001.jpg": lambda: b"\x00\x05\x16\x07" + b"\x00" * 50,
    "junk/Thumbs.db": lambda: b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 50,
    "2020/holiday/IMG_0001.xmp": lambda: b"<?xpacket begin=''?><x:xmpmeta/>",
    "mystery.bin": lambda: bytes(range(256)) * 2,
    "root_photo.jpg": lambda: jpeg_with_exif(make="Other"),
}


def build_tree(root: Path) -> dict[str, Path]:
    paths = {}
    for rel, make in SAMPLES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(make())
        paths[rel] = p
    return paths


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    build_tree(src)
    return src


def write_config(tmp_path: Path, src: Path, **catalogue) -> Path:
    cat = {"workers": 2, "use_exiftool": False, **catalogue}
    cat_yaml = "\n".join(f"  {k}: {str(v).lower() if isinstance(v, bool) else v}" for k, v in cat.items())
    cfg = tmp_path / "imagepipe.yml"
    cfg.write_text(f"""
timezone: Australia/Brisbane
manifest: {tmp_path}/data/imagepipe.db
log_dir: {tmp_path}/data/logs
reports_dir: {tmp_path}/data/reports
sources:
  - name: test
    path: {src}
    host_path: /host/photos
catalogue:
{cat_yaml}
""")
    return cfg


@pytest.fixture
def cfg(tmp_path, tree):
    return load_config(write_config(tmp_path, tree))
