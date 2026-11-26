"""Layered TIFFs and Photoshop files (PSD/PSB).

Two jobs:

* ``find`` - a read-only review list of every PSD/PSB, and every TIFF that is layered
  (it carries Photoshop layer data) or that Pillow can't read, so you can decide
  whether they belong in the photo pipeline.
* ``render`` - a full-resolution working copy (JPEG, or PNG when there is
  transparency; CMYK and 16-bit converted to normal 8-bit RGB) from a PSD/PSB or a
  TIFF. Nothing in the pipeline calls it yet; it is here for when these files are
  sent through ImagePipe, the same way as RAW working copies.

psd-tools reads PSD/PSB files (the stored merged image, or a composite of the layers
when there is none). TIFFs are read with Pillow, falling back to tifffile for
encodings Pillow doesn't handle. Both are in the ``layered`` extra:
``pip install "imagepipe[layered]"``.
"""

from __future__ import annotations

import csv
import hashlib
import os
import struct
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image

from imagepipe.progress import Progress, fmt_bytes

PSD_EXT = {"psd", "psb"}
TIFF_EXT = {"tif", "tiff"}
LAYERED_EXT = PSD_EXT | TIFF_EXT

PSD_MODES = {0: "Bitmap", 1: "Grayscale", 2: "Indexed", 3: "RGB", 4: "CMYK", 7: "Multichannel",
             8: "Duotone", 9: "Lab"}
TIFF_PHOTOMETRIC = {0: "Grayscale", 1: "Grayscale", 2: "RGB", 3: "Indexed", 4: "Mask", 5: "CMYK",
                    6: "YCbCr", 8: "Lab"}
TAG_PHOTOSHOP_LAYERS = 37724   # ImageSourceData: Photoshop layers inside a TIFF
TAG_PHOTOSHOP_RESOURCES = 34377

CSV_FIELDS = ["path", "type", "why_listed", "width", "height", "colour_mode", "bit_depth", "layers",
              "has_merged_image", "pillow_can_read", "size_bytes", "modified", "sha256", "copies", "note"]


class MissingDependency(RuntimeError):
    pass


def _psd_tools():
    try:
        from psd_tools import PSDImage
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise MissingDependency('psd-tools is not installed: pip install "imagepipe[layered]"') from exc
    return PSDImage


# ----------------------------------------------------------------- inspecting
def _psd_header(path: str) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(26)
    if len(head) < 26 or head[:4] != b"8BPS":
        raise ValueError("not a Photoshop file (no 8BPS header)")
    version, = struct.unpack(">H", head[4:6])
    channels, height, width, depth, mode = struct.unpack(">HIIHH", head[12:26])
    return {"type": "psb" if version == 2 else "psd", "width": width, "height": height, "bit_depth": depth,
            "colour_mode": PSD_MODES.get(mode, f"mode {mode}"), "channels": channels}


def _tiff_first_ifd(path: str) -> dict:
    """Size, depth, colour and Photoshop tags from a TIFF's first page, without decoding it."""
    with open(path, "rb") as fh:
        head = fh.read(8)
        if head[:4] in (b"II\x2b\x00", b"MM\x00\x2b"):
            return {"note": "BigTIFF: header not read"}
        if head[:4] not in (b"II\x2a\x00", b"MM\x00\x2a"):
            raise ValueError("not a TIFF file")
        e = "<" if head[:2] == b"II" else ">"
        offset, = struct.unpack(e + "I", head[4:8])
        fh.seek(offset)
        n, = struct.unpack(e + "H", fh.read(2))
        tags: dict[int, tuple[int, int, bytes]] = {}
        for _ in range(min(n, 4096)):
            raw = fh.read(12)
            if len(raw) < 12:
                break
            tag, typ, count = struct.unpack(e + "HHI", raw[:8])
            tags[tag] = (typ, count, raw[8:12])
        pages = 1
        nxt_raw = fh.read(4)
        nxt = struct.unpack(e + "I", nxt_raw)[0] if len(nxt_raw) == 4 else 0
        size = os.fstat(fh.fileno()).st_size

        def first(tag):
            """The tag's first value (SHORT/LONG), following the offset when values don't fit inline."""
            if tag not in tags:
                return None
            typ, count, val = tags[tag]
            width = {3: 2, 4: 4}.get(typ)
            if width is None:
                return None
            if count * width > 4:
                fh.seek(struct.unpack(e + "I", val)[0])
                val = fh.read(width)
                if len(val) < width:
                    return None
            return struct.unpack(e + ("H" if width == 2 else "I"), val[:width])[0]

        bits, samples = first(258), first(277) or 1
        width, height, photometric = first(256), first(257), first(262)
        seen = {offset}
        while nxt and nxt < size and nxt not in seen and pages < 1000:
            seen.add(nxt)
            fh.seek(nxt)
            raw = fh.read(2)
            if len(raw) < 2:
                break
            cnt, = struct.unpack(e + "H", raw)
            fh.seek(nxt + 2 + 12 * cnt)
            nr = fh.read(4)
            nxt = struct.unpack(e + "I", nr)[0] if len(nr) == 4 else 0
            pages += 1

    return {
        "width": width, "height": height,
        "bit_depth": bits,
        "colour_mode": TIFF_PHOTOMETRIC.get(photometric, "unknown"),
        "samples": samples, "pages": pages,
        "photoshop_layers": TAG_PHOTOSHOP_LAYERS in tags,
        "photoshop_resources": TAG_PHOTOSHOP_RESOURCES in tags,
    }


def _pillow_can_read(path: str) -> bool:
    try:
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:  # noqa: BLE001 - any failure means "Pillow can't read it"
        return False


def inspect(path: str) -> dict:
    """What a review needs to know about one PSD/PSB/TIFF. Never raises: problems go in 'note'."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
    info: dict = {"path": path, "type": ext, "why_listed": "", "layers": "", "has_merged_image": "",
                  "pillow_can_read": "", "note": ""}
    try:
        st = os.stat(path)
        info["size_bytes"] = st.st_size
        info["modified"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError as exc:
        info["note"] = f"stat: {exc}"
        return info
    try:
        if ext in PSD_EXT:
            info.update(_psd_header(path))
            info["why_listed"] = "photoshop"
            try:
                psd = _psd_tools().open(path)
                info["layers"] = sum(1 for layer in psd.descendants() if not layer.is_group())
                info["has_merged_image"] = "yes" if psd.has_preview() else "no"
            except MissingDependency as exc:
                info["note"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                info["note"] = f"psd-tools: {type(exc).__name__}: {exc}"[:300]
            info["pillow_can_read"] = "yes" if _pillow_can_read(path) else "no"
        elif ext in TIFF_EXT:
            t = _tiff_first_ifd(path)
            info.update({k: t.get(k) for k in ("width", "height", "bit_depth", "colour_mode") if k in t})
            if t.get("note"):
                info["note"] = t["note"]
            readable = _pillow_can_read(path)
            info["pillow_can_read"] = "yes" if readable else "no"
            reasons = []
            if t.get("photoshop_layers"):
                reasons.append("layered")
                info["layers"] = "yes"
            if t.get("pages", 1) > 1:
                reasons.append(f"{t['pages']} pages")
            if not readable:
                reasons.append("pillow-cannot-read")
            info["why_listed"] = ",".join(reasons)
            info["has_merged_image"] = "yes"  # a TIFF's first page is always the flattened picture
        else:
            info["note"] = "not a PSD/PSB/TIFF"
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"{type(exc).__name__}: {exc}"[:300]
        info["why_listed"] = info["why_listed"] or "unreadable"
    return info


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find(roots: list[Path], out_dir: Path, tz=None, quiet: bool = False) -> dict:
    """Walk `roots`, list PSD/PSB and layered/unreadable TIFFs, write layered.csv + summary.md."""
    candidates: list[str] = []
    with Progress("1/2 Find", unit="files") as p:
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    p.update(1)
                    if "." in name and name.rsplit(".", 1)[-1].lower() in LAYERED_EXT:
                        candidates.append(os.path.join(dirpath, name))
            p.set_extra(f"{len(candidates):,} PSD/TIFF found")
    total_bytes = sum(os.path.getsize(c) for c in candidates if os.path.exists(c))
    rows: list[dict] = []
    with Progress("2/2 Inspect", total=len(candidates), total_bytes=total_bytes) as p:
        for path in candidates:
            info = inspect(path)
            if info["type"] in PSD_EXT or info["why_listed"]:
                try:
                    info["sha256"] = _sha256(path)
                except OSError as exc:
                    info["note"] = (info["note"] + f"; sha256: {exc}").strip("; ")
                rows.append(info)
            p.update(1, info.get("size_bytes") or 0, extra=f"{len(rows):,} listed")
    copies = Counter(r.get("sha256") for r in rows if r.get("sha256"))
    for r in rows:
        r["copies"] = copies.get(r.get("sha256"), 1)

    stamp = datetime.now(tz).strftime("%Y%m%d-%H%M%S")
    folder = out_dir / f"layered-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "layered.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x.get("sha256") or "", x["path"])):
            w.writerow(r)
    unique = len(copies) + sum(1 for r in rows if not r.get("sha256"))
    by_type = Counter(r["type"] for r in rows)
    why = Counter(reason for r in rows for reason in (r["why_listed"] or "").split(",") if reason)
    lines = [
        f"# Layered TIFF / PSD review ({stamp})", "",
        f"- Searched: {', '.join(f'`{r}`' for r in roots)}",
        f"- Listed: {len(rows):,} files ({fmt_bytes(sum(r.get('size_bytes') or 0 for r in rows))}), "
        f"{unique:,} different (the rest are identical copies)",
        f"- PSD/TIFF files looked at: {len(candidates):,}", "",
        "| Type | Files |", "|---|---:|", *[f"| {k} | {v:,} |" for k, v in sorted(by_type.items())], "",
        "| Why listed | Files |", "|---|---:|", *[f"| {k} | {v:,} |" for k, v in sorted(why.items())], "",
        "Every file is in `layered.csv` (identical copies next to each other): size, colour mode, bit depth, "
        "layer count, whether a merged image is stored, and whether Pillow can read it.",
        "To see what one would look like in the pipeline: `imagepipe layered render <file>`.",
    ]
    (folder / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not quiet:
        print("\n".join(lines))
        print(f"\nReport written to: {folder}")
    return {"listed": len(rows), "unique": unique, "looked_at": len(candidates), "folder": str(folder)}


# ----------------------------------------------------------------- working copies
def _to_8bit_rgb(im: Image.Image) -> Image.Image:
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        im = im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if im.mode == "CMYK":
        return im.convert("RGB")
    if im.mode in ("RGBA", "LA", "PA"):
        return im.convert("RGBA")
    if im.mode == "P":
        return im.convert("RGBA" if "transparency" in im.info else "RGB")
    if im.mode not in ("RGB", "L"):
        return im.convert("RGB")
    return im


def _tiff_with_tifffile(path: str) -> Image.Image:
    try:
        import numpy as np
        import tifffile
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise MissingDependency('tifffile is not installed: pip install "imagepipe[layered]"') from exc
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        arr = page.asarray()
        photometric = int(page.photometric)
    if arr.dtype != np.uint8:
        top = float(np.iinfo(arr.dtype).max) if np.issubdtype(arr.dtype, np.integer) else float(arr.max() or 1)
        arr = (arr.astype("float64") / top * 255).clip(0, 255).astype("uint8")
    if arr.ndim == 3 and arr.shape[0] in (1, 3, 4, 5) and arr.shape[-1] > 5:  # planar -> interleaved
        arr = np.moveaxis(arr, 0, -1)
    if arr.ndim == 2:
        return Image.fromarray(arr, "L")
    if photometric == 5:  # CMYK
        return Image.fromarray(np.ascontiguousarray(arr[..., :4]), "CMYK").convert("RGB")
    bands = arr.shape[-1]
    if bands >= 4:
        return Image.fromarray(np.ascontiguousarray(arr[..., :4]), "RGBA")
    if bands == 3:
        return Image.fromarray(np.ascontiguousarray(arr), "RGB")
    return Image.fromarray(np.ascontiguousarray(arr[..., 0]), "L")


def flatten(path: str) -> tuple[Image.Image, str]:
    """(the picture, how it was made) for a PSD/PSB/TIFF."""
    ext = path.rsplit(".", 1)[-1].lower()
    if ext in PSD_EXT:
        psd = _psd_tools().open(path)
        how = "psd merged image" if psd.has_preview() else "psd composited from layers"
        return psd.composite(), how
    try:
        with Image.open(path) as im:
            im.load()
            return im.copy(), "tiff (pillow)"
    except Exception:  # noqa: BLE001
        return _tiff_with_tifffile(path), "tiff (tifffile)"


def render(path: str, out_dir: Path, quality: int = 95) -> tuple[Path, str]:
    """Write a full-resolution working copy to <out_dir>/<stem>.jpg (or .png with transparency)."""
    im, how = flatten(path)
    im = _to_8bit_rgb(im)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(path).stem
    if im.mode == "RGBA" and im.getextrema()[3][0] < 255:
        dest = out_dir / f"{stem}.png"
        im.save(dest, "PNG")
    else:
        dest = out_dir / f"{stem}.jpg"
        im.convert("RGB").save(dest, "JPEG", quality=quality, subsampling=0)
    return dest, how
