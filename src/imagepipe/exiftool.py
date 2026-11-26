"""A small persistent ExifTool wrapper (-stay_open), one process per worker."""

from __future__ import annotations

import json
import shutil
import subprocess

TAGS = ("-Make", "-Model", "-DateTimeOriginal", "-CreateDate", "-ImageWidth", "-ImageHeight")


def available(exe: str = "exiftool") -> bool:
    return shutil.which(exe) is not None


class ExifTool:
    def __init__(self, exe: str = "exiftool"):
        self.proc = subprocess.Popen(
            [exe, "-stay_open", "True", "-@", "-", "-common_args", "-j", "-fast2", "-charset", "filename=utf8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read(self, path: str) -> dict:
        """Return a dict of the TAGS for one file ({} if exiftool can't read it)."""
        if "\n" in path or "\r" in path:
            return {}
        args = "\n".join(TAGS + (path, "-execute")) + "\n"
        self.proc.stdin.write(args.encode("utf-8", "surrogateescape"))
        self.proc.stdin.flush()
        out = bytearray()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("exiftool exited unexpectedly")
            if line.strip() == b"{ready}":
                break
            out += line
        if not out.strip():
            return {}
        try:
            data = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {}
        return data[0] if data else {}

    def close(self) -> None:
        try:
            self.proc.stdin.write(b"-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
