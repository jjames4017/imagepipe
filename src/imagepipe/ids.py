"""Pipeline IDs, shared by triage and the ImagePipe stages.

Format: "IP" followed by 16 Crockford base32 characters, e.g. IP01JDX4Q8M3K7ZB2R.

- The first 9 characters are the creation time in milliseconds, so IDs sort by
  the order they were made.
- The last 7 characters are random (35 bits). Within one millisecond the random
  part counts up, so one process never makes the same ID twice.

An ID is written into a file's own metadata as XMP-dc:Identifier and put at the
front of its file name (<ID>_...). Any stage that finds an existing ID keeps it.
"""

from __future__ import annotations

import os
import re
import threading
import time

PREFIX = "IP"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RAND_BITS = 35
ID_PATTERN = r"IP[0-9A-HJKMNP-TV-Z]{16}"
ID_RE = re.compile(rf"^{ID_PATTERN}$")
NAME_ID_RE = re.compile(rf"^({ID_PATTERN})(?:_|\.|$)")

_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(ALPHABET[rem])
    return "".join(reversed(chars))


def new_id(now_ms: int | None = None) -> str:
    """Make a new pipeline ID."""
    global _last_ms, _last_rand
    with _lock:
        ms = int(time.time() * 1000) if now_ms is None else now_ms
        if ms <= _last_ms:
            ms = _last_ms
            rand = _last_rand + 1
            if rand >= 1 << RAND_BITS:
                ms += 1
                rand = int.from_bytes(os.urandom(5), "big") >> (40 - RAND_BITS)
        else:
            rand = int.from_bytes(os.urandom(5), "big") >> (40 - RAND_BITS)
        _last_ms, _last_rand = ms, rand
    return PREFIX + _encode(ms, 9) + _encode(rand, 7)


def is_id(value: str | None) -> bool:
    return bool(value) and bool(ID_RE.match(value))


def id_from_name(name: str) -> str | None:
    """The pipeline ID at the front of a file name, if it has one."""
    m = NAME_ID_RE.match(os.path.basename(name))
    return m.group(1) if m else None
