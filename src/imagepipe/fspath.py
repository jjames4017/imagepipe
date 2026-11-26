"""File names that aren't valid UTF-8.

Linux file names are bytes. Python reads a name that isn't valid UTF-8 (for
example one made by an old Windows or Mac machine in Latin-1) with each bad byte
turned into a "surrogate" character (U+DC80-U+DCFF). SQLite won't store those.

to_db() swaps each of them for a character in a private-use range
(U+F780-U+F7FF) that SQLite stores fine; to_fs() swaps them back, so the file
can be opened and moved under its exact original name. Names that are valid
UTF-8 - nearly all of them - pass through unchanged.
"""

from __future__ import annotations

import re

_SHIFT = 0xF700 - 0xDC00
_SURROGATE = re.compile("[\udc80-\udcff]")
_PRIVATE = re.compile("[-]")


def to_db(path):
    """A path as stored in the database (always valid UTF-8)."""
    if path is None:
        return None
    path = str(path)
    if not _SURROGATE.search(path):
        return path
    return _SURROGATE.sub(lambda m: chr(ord(m.group()) + _SHIFT), path)


def to_fs(path):
    """A path from the database, ready to open on disk."""
    if path is None:
        return None
    path = str(path)
    if not _PRIVATE.search(path):
        return path
    return _PRIVATE.sub(lambda m: chr(ord(m.group()) - _SHIFT), path)


def has_bad_bytes(path) -> bool:
    return bool(path) and bool(_PRIVATE.search(str(path)) or _SURROGATE.search(str(path)))
