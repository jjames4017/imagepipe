"""A small persistent ExifTool wrapper (-stay_open), one process per worker."""

from __future__ import annotations

import json
import shutil
import subprocess

TAGS = ("-Make", "-Model", "-DateTimeOriginal", "-CreateDate", "-ImageWidth", "-ImageHeight")
READ_ARGS = ("-j", "-fast2")


def available(exe: str = "exiftool") -> bool:
    return shutil.which(exe) is not None


class ExifTool:
    """One long-running exiftool process.

    read=True (the default) starts it in JSON read mode; read=False starts it
    for writing tags.
    """

    def __init__(self, exe: str = "exiftool", read: bool = True):
        common = list(READ_ARGS) if read else []
        self.proc = subprocess.Popen(
            [exe, "-stay_open", "True", "-@", "-", "-common_args", *common, "-charset", "filename=utf8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _execute(self, args: list[str]) -> bytes:
        self.proc.stdin.write(("\n".join(args + ["-execute"]) + "\n").encode("utf-8", "surrogateescape"))
        self.proc.stdin.flush()
        out = bytearray()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("exiftool exited unexpectedly")
            if line.strip() == b"{ready}":
                break
            out += line
        return bytes(out)

    def read(self, path: str, tags: tuple[str, ...] = TAGS) -> dict:
        """Return a dict of the tags for one file ({} if exiftool can't read it)."""
        if "\n" in path or "\r" in path:
            return {}
        out = self._execute(list(tags) + [path])
        if not out.strip():
            return {}
        try:
            data = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {}
        return data[0] if data else {}

    def write(self, path: str, assignments: dict[str, str]) -> tuple[bool, str]:
        """Write tags into a file in place, keeping its modified time.

        assignments maps tag names (e.g. "XMP-dc:Identifier") to values.
        Returns (ok, exiftool's summary line).
        """
        if "\n" in path or "\r" in path:
            return False, "path contains a newline"
        args = ["-overwrite_original", "-P"]
        for tag, value in assignments.items():
            value = str(value).replace("\r", " ").replace("\n", " ")
            args.append(f"-{tag}={value}")
        args.append(path)
        out = self._execute(args).decode("utf-8", "replace").strip()
        ok = "1 image files updated" in out or "1 files updated" in out
        return ok, " ".join(out.split())[:300]

    def close(self) -> None:
        try:
            self.proc.stdin.write(b"-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
