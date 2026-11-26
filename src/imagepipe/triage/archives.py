"""Extracting archives, with every file checked before the archive is deleted.

Supported: .zip (Python), .tar/.tgz/.tar.bz2/.tar.xz and single .gz/.bz2/.xz
files (Python), .7z/.rar/.cab (7-Zip: the `7zz` or `7z` command).

Checks:
- zip: each file's CRC-32 (checked while it is read) and size
- tar: each file's size
- gz/bz2/xz: the format's own checksum (checked while decompressing)
- 7z/rar/cab: 7-Zip's own test of the whole archive, then each file's size
Afterwards the caller reads every extracted file again and compares its SHA-256
with the one taken while extracting.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import os
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from imagepipe.progress import Progress
from imagepipe.triage.kinds import archive_stem

CHUNK = 1 << 20


class ArchiveError(Exception):
    """The archive can't be extracted. reason is a quarantine reason."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class NoSpace(Exception):
    pass


@dataclass
class Extracted:
    name: str      # path inside the archive (cleaned)
    path: Path     # where it was written
    size: int
    sha256: str
    mtime: float


def seven_zip() -> str | None:
    return shutil.which("7zz") or shutil.which("7z")


def safe_member_name(name: str) -> str:
    """A member path that can't escape the extraction folder."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


class _Namer:
    def __init__(self):
        self.used: set[str] = set()

    def __call__(self, name: str) -> str:
        clean = safe_member_name(name) or "unnamed"
        out, n = clean, 1
        while out.lower() in self.used:
            head, dot, ext = clean.rpartition(".")
            out = f"{head}-dup{n}.{ext}" if dot and "/" not in ext else f"{clean}-dup{n}"
            n += 1
        self.used.add(out.lower())
        return out


def _check_space(dest: Path, need: int) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest).free
    if free < need * 1.05 + (1 << 30):
        raise NoSpace(f"Not enough free space in {dest} to extract {need:,} bytes (free: {free:,}).")


def _write_stream(src, out: Path, progress: Progress | None) -> tuple[int, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with open(out, "xb") as fo:
        while True:
            block = src.read(CHUNK)
            if not block:
                break
            fo.write(block)
            digest.update(block)
            size += len(block)
            if progress is not None:
                progress.update(0, len(block))
        fo.flush()
        os.fsync(fo.fileno())
    return size, digest.hexdigest()


def _zip(path: str, dest: Path, max_bytes: int, progress) -> list[Extracted]:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError("archive-failed", f"not a readable zip: {exc}")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if any(i.flag_bits & 0x1 for i in infos):
            raise ArchiveError("encrypted", "zip is password protected")
        total = sum(i.file_size for i in infos)
        if total > max_bytes:
            raise ArchiveError("archive-failed", f"unpacks to {total:,} bytes, over max_extract_bytes")
        _check_space(dest, total)
        out, namer = [], _Namer()
        for info in infos:
            name = namer(info.filename)
            target = dest / name
            try:
                with zf.open(info) as src:
                    size, sha = _write_stream(src, target, progress)
            except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError, RuntimeError) as exc:
                raise ArchiveError("archive-failed", f"{info.filename}: {exc}")
            if size != info.file_size:
                raise ArchiveError("archive-failed", f"{info.filename}: size {size} != {info.file_size}")
            try:
                mtime = datetime(*info.date_time).timestamp()
            except (ValueError, OverflowError):
                mtime = time.time()
            os.utime(target, (mtime, mtime))
            out.append(Extracted(name, target, size, sha, mtime))
            if progress is not None:
                progress.update(1)
        return out


def _tar(tf: tarfile.TarFile, dest: Path, max_bytes: int, progress) -> list[Extracted]:
    members = [m for m in tf.getmembers() if m.isfile()]
    total = sum(m.size for m in members)
    if total > max_bytes:
        raise ArchiveError("archive-failed", f"unpacks to {total:,} bytes, over max_extract_bytes")
    _check_space(dest, total)
    out, namer = [], _Namer()
    for m in members:
        name = namer(m.name)
        target = dest / name
        try:
            src = tf.extractfile(m)
            if src is None:
                continue
            with src:
                size, sha = _write_stream(src, target, progress)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ArchiveError("archive-failed", f"{m.name}: {exc}")
        if size != m.size:
            raise ArchiveError("archive-failed", f"{m.name}: size {size} != {m.size}")
        os.utime(target, (m.mtime, m.mtime))
        out.append(Extracted(name, target, size, sha, float(m.mtime)))
        if progress is not None:
            progress.update(1)
    return out


def _single(path: str, opener, dest: Path, max_bytes: int, progress) -> list[Extracted]:
    name = safe_member_name(archive_stem(os.path.basename(path))) or "unpacked"
    target = dest / name
    _check_space(dest, os.path.getsize(path) * 4)
    try:
        with opener(path, "rb") as src:
            size, sha = _write_stream(src, target, progress)
    except (OSError, EOFError, lzma.LZMAError, ValueError) as exc:
        raise ArchiveError("archive-failed", f"decompress failed: {exc}")
    if size > max_bytes:
        raise ArchiveError("archive-failed", f"unpacks to {size:,} bytes, over max_extract_bytes")
    mtime = os.path.getmtime(path)
    os.utime(target, (mtime, mtime))
    if progress is not None:
        progress.update(1)
    return [Extracted(name, target, size, sha, mtime)]


def _7z_list(exe: str, path: str) -> list[dict]:
    res = subprocess.run([exe, "l", "-slt", "-p-", path], capture_output=True, text=True, errors="replace")
    text = res.stdout + res.stderr
    if "Wrong password" in text or "encrypted" in text.lower() and res.returncode != 0:
        raise ArchiveError("encrypted", "archive is password protected")
    if res.returncode != 0:
        raise ArchiveError("archive-failed", f"7-Zip could not list it: {text.strip()[-300:]}")
    entries, cur, started = [], {}, False
    for line in res.stdout.splitlines():
        if line.startswith("----------"):
            started = True
            continue
        if not started:
            continue
        if not line.strip():
            if cur.get("Path"):
                entries.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" = ")
        cur[k.strip()] = v
    if cur.get("Path"):
        entries.append(cur)
    files = []
    for e in entries:
        if e.get("Folder") == "+" or "D" in e.get("Attributes", "")[:1]:
            continue
        if e.get("Encrypted") == "+":
            raise ArchiveError("encrypted", "archive is password protected")
        files.append({"name": e["Path"], "size": int(e.get("Size") or 0)})
    return files


def _poll_size(folder: Path, progress: Progress, stop: threading.Event) -> None:
    last = 0
    while not stop.wait(2):
        total = 0
        for root, _dirs, files in os.walk(folder):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        progress.update(0, total - last)
        last = total


def _sevenzip(path: str, dest: Path, max_bytes: int, progress) -> list[Extracted]:
    exe = seven_zip()
    if exe is None:
        raise ArchiveError("archive-failed", "7-Zip (7zz or 7z) is not installed")
    files = _7z_list(exe, path)
    total = sum(f["size"] for f in files)
    if total > max_bytes:
        raise ArchiveError("archive-failed", f"unpacks to {total:,} bytes, over max_extract_bytes")
    _check_space(dest, total)
    test = subprocess.run([exe, "t", "-p-", path], capture_output=True, text=True, errors="replace")
    if test.returncode != 0:
        raise ArchiveError("archive-failed", f"7-Zip test failed: {(test.stdout + test.stderr).strip()[-300:]}")
    stop = threading.Event()
    poller = None
    if progress is not None:
        poller = threading.Thread(target=_poll_size, args=(dest, progress, stop), daemon=True)
        poller.start()
    try:
        res = subprocess.run([exe, "x", "-p-", "-y", "-bso0", "-bsp0", f"-o{dest}", path],
                             capture_output=True, text=True, errors="replace")
    finally:
        stop.set()
        if poller is not None:
            poller.join()
    if res.returncode != 0:
        raise ArchiveError("archive-failed", f"7-Zip extract failed: {res.stderr.strip()[-300:]}")
    out = []
    real_dest = os.path.realpath(dest)
    for f in files:
        name = safe_member_name(f["name"])
        target = dest / name
        if os.path.islink(target) or not os.path.realpath(target).startswith(real_dest + os.sep):
            raise ArchiveError("archive-failed", f"{f['name']}: unsafe path or link")
        if not target.is_file():
            raise ArchiveError("archive-failed", f"{f['name']}: missing after extraction")
        st = target.stat()
        if st.st_size != f["size"]:
            raise ArchiveError("archive-failed", f"{f['name']}: size {st.st_size} != {f['size']}")
        digest = hashlib.sha256()
        with open(target, "rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                digest.update(block)
        out.append(Extracted(name, target, st.st_size, digest.hexdigest(), st.st_mtime))
        if progress is not None:
            progress.update(1)
    for root, _dirs, names in os.walk(dest):
        for n in names:
            if os.path.islink(os.path.join(root, n)):
                raise ArchiveError("archive-failed", f"archive contains a link: {n}")
    return out


def extract(path: str, mime: str, dest: Path, max_bytes: int, progress: Progress | None = None) -> list[Extracted]:
    """Extract an archive into dest (which must not exist yet). Raises ArchiveError or NoSpace."""
    if dest.exists():
        raise ArchiveError("archive-failed", f"extraction folder already exists: {dest}")
    if mime == "application/zip":
        return _zip(path, dest, max_bytes, progress)
    if mime in ("application/x-tar", "application/gzip", "application/x-bzip2", "application/x-xz"):
        try:
            tf = tarfile.open(path, "r:*")
        except (tarfile.TarError, OSError, EOFError, lzma.LZMAError):
            tf = None
        if tf is not None:
            with tf:
                return _tar(tf, dest, max_bytes, progress)
        opener = {"application/gzip": gzip.open, "application/x-bzip2": bz2.open,
                  "application/x-xz": lzma.open}.get(mime)
        if opener is None:
            raise ArchiveError("archive-failed", "not a readable tar")
        return _single(path, opener, dest, max_bytes, progress)
    if mime in ("application/vnd.rar", "application/x-7z-compressed", "application/vnd.ms-cab-compressed"):
        return _sevenzip(path, dest, max_bytes, progress)
    raise ArchiveError("archive-failed", f"unsupported archive type {mime}")
