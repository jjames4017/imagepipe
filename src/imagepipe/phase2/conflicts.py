"""The conflict review (#57): date/time (including time zone) and GPS values that disagree.

A conflict is raised when two sources for the same photo disagree: two copies of the same
picture (pixel-identical duplicates), or the photo and its Google Takeout .json. Nothing is
written for that field until you pick a value in the Conflicts tab; the photo waits as
pending and the run carries on with the others.

Similar conflicts share a signature: the same field, the same kinds of source and (for
dates) the same difference, e.g. "camera time vs Takeout time, 10 hours apart". "Apply this
choice to all similar conflicts" picks the same kind of source in every one of them.

GPS recommendations come from photos taken by the same camera within an hour; they are
only ever a suggestion. (An image-based estimate arrives with stage 8, GeoCLIP.)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from imagepipe.phase2 import meta

NEIGHBOUR_WINDOW = timedelta(hours=1)
NEIGHBOUR_KM = 1.0


# -- comparing ---------------------------------------------------------------------------
def date_delta_minutes(a: dict, b: dict) -> int | None:
    """Minutes between two date candidates as written (local vs UTC counts as a difference)."""
    da, db = meta.to_utc(a["date"], a.get("offset")), meta.to_utc(b["date"], b.get("offset"))
    if da is None or db is None:
        return None
    return round((da.replace(tzinfo=None) - db.replace(tzinfo=None)).total_seconds() / 60)


def dates_agree(a: dict, b: dict) -> bool:
    delta = date_delta_minutes(a, b)
    return delta is not None and abs(delta) < 1


def gps_agree(a: tuple, b: tuple, metres: float = 50) -> bool:
    return km(a, b) * 1000 <= metres


def km(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _tz_like(minutes: int) -> bool:
    return minutes != 0 and abs(minutes) <= 14 * 60 and minutes % 15 == 0


# -- making ---------------------------------------------------------------------------------
def date_conflict(cands: list[dict]) -> tuple[str, dict]:
    """(signature, recommendation) for disagreeing date candidates.

    Each candidate: {key, source, date, offset, file_id}.
    """
    first, second = cands[0], cands[1]
    delta = date_delta_minutes(first, second)
    label = meta.fmt_offset(delta) if delta is not None and _tz_like(delta) else "other"
    signature = "date|" + "|".join(sorted(c["key"] for c in cands)) + "|" + label
    # Same moment, one in local time and one in UTC: keep the local time, add the time zone
    local = [c for c in cands if not c.get("offset")]
    zoned = [c for c in cands if c.get("offset")]
    if len(cands) == 2 and len(local) == 1 and len(zoned) == 1:
        d = date_delta_minutes(local[0], zoned[0])
        if d is not None and _tz_like(d):
            off_min = d + _offset_minutes(zoned[0]["offset"])
            if abs(off_min) <= 14 * 60:
                return signature, {"key": "recommended", "date": local[0]["date"], "offset": meta.fmt_offset(off_min),
                                   "why": f"The same moment: {local[0]['source']} in local time, {zoned[0]['source']} "
                                          f"in UTC{zoned[0]['offset']}. Keeps the local time and adds the time zone "
                                          f"(UTC{meta.fmt_offset(off_min)})."}
    camera = next((c for c in cands if c["key"].startswith("exif")), None)
    if camera:
        return signature, {**camera, "key": camera["key"],
                           "why": "The camera's own date (EXIF DateTimeOriginal) of the kept copy"}
    earliest = min(cands, key=lambda c: meta.to_utc(c["date"], c.get("offset")).replace(tzinfo=None))
    return signature, {**earliest, "why": "The earliest of the dates"}


def _offset_minutes(offset: str | None) -> int:
    if not offset:
        return 0
    sign = 1 if offset[0] == "+" else -1
    return sign * (int(offset[1:3]) * 60 + int(offset[4:6]))


def gps_conflict(db, cands: list[dict], model: str | None, date: str | None,
                 exclude_ids: set[int]) -> tuple[str, dict | None]:
    """(signature, recommendation or None) for disagreeing GPS candidates ({key, source, lat, lon})."""
    signature = "gps|" + "|".join(sorted(c["key"] for c in cands))
    when = meta.to_utc(date, None) if date else None
    if not (model and when):
        return signature, None
    lo, hi = when - NEIGHBOUR_WINDOW, when + NEIGHBOUR_WINDOW
    rows = db.execute(
        "SELECT id, gps_lat, gps_lon, date_taken FROM files WHERE camera_model=? AND gps_lat IS NOT NULL "
        "AND date_taken BETWEEN ? AND ? AND status != 'excluded'",
        (model, lo.strftime("%Y:%m:%d %H:%M:%S"), hi.strftime("%Y:%m:%d %H:%M:%S"))).fetchall()
    points = [(r["gps_lat"], r["gps_lon"]) for r in rows if r["id"] not in exclude_ids]
    if not points:
        return signature, None
    best, support = None, -1
    for c in cands:
        n = sum(1 for p in points if km(p, (c["lat"], c["lon"])) <= NEIGHBOUR_KM)
        if n > support:
            best, support = c, n
    if support <= 0:
        return signature, None
    conf = round(100 * support / len(points))
    return signature, {**best, "confidence": conf,
                       "why": f"{support} of {len(points)} photo(s) from the same camera within an hour were taken "
                              f"within {NEIGHBOUR_KM:g} km of this location"}


def add(m, file_id: int, field: str, signature: str, cands: list[dict], rec: dict | None) -> int:
    cur = m.db.execute(
        "INSERT INTO conflicts (file_id, field, signature, candidates, recommended, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (file_id, field, signature, json.dumps(cands), json.dumps(rec) if rec else None, m.now()))
    return cur.lastrowid


# -- reading ----------------------------------------------------------------------------------
def listing(db, status: str = "pending", field: str = "", limit: int = 50, offset: int = 0) -> dict:
    where, params = ["c.status=?"], [status]
    if field:
        where.append("c.field=?")
        params.append(field)
    w = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM conflicts c WHERE {w}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT c.*, f.pipeline_id, f.current_path, f.batch, f.status AS file_status FROM conflicts c "
        f"JOIN files f ON f.id = c.file_id WHERE {w} ORDER BY c.signature, c.id LIMIT ? OFFSET ?",
        (*params, limit, offset)).fetchall()
    sigs = {r["signature"]: r["n"] for r in db.execute(
        "SELECT signature, COUNT(*) AS n FROM conflicts WHERE status='pending' GROUP BY signature")}
    out = []
    for r in rows:
        d = dict(r)
        d["candidates"] = json.loads(d["candidates"])
        d["recommended"] = json.loads(d["recommended"]) if d["recommended"] else None
        d["chosen"] = json.loads(d["chosen"]) if d["chosen"] else None
        d["similar"] = sigs.get(d["signature"], 1) - (1 if d["status"] == "pending" else 0)
        out.append(d)
    counts = {r[0]: r[1] for r in db.execute("SELECT field, COUNT(*) FROM conflicts WHERE status='pending' "
                                             "GROUP BY field")}
    return {"rows": out, "total": total, "pending": counts}


# -- resolving ----------------------------------------------------------------------------------
def _pick(conflict, choice: str) -> dict | None:
    if choice == "recommended":
        return json.loads(conflict["recommended"]) if conflict["recommended"] else None
    for c in json.loads(conflict["candidates"]):
        if c["key"] == choice:
            return c
    return None


def resolve(m, tools, conflict_id: int, choice: str, apply_similar: bool = False) -> dict:
    """Write the chosen value into the kept file. Returns {resolved, failed: [...]}."""
    first = m.db.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
    if first is None:
        raise ValueError(f"No conflict {conflict_id}")
    if first["status"] != "pending":
        raise ValueError("That conflict has already been resolved")
    if _pick(first, choice) is None:
        raise ValueError(f"'{choice}' isn't one of this conflict's values")
    todo = [first]
    if apply_similar:
        todo += m.db.execute("SELECT * FROM conflicts WHERE status='pending' AND signature=? AND id != ? ORDER BY id",
                             (first["signature"], first["id"])).fetchall()
    resolved, failed, skipped = 0, [], 0
    for c in todo:
        value = _pick(c, choice)
        if value is None:  # e.g. "recommended" where a similar conflict has no recommendation
            skipped += 1
            continue
        f = m.db.execute("SELECT * FROM files WHERE id=?", (c["file_id"],)).fetchone()
        ok, msg = write_choice(tools, f, c["field"], value)
        if not ok:
            failed.append({"id": c["id"], "path": f["current_path"], "why": msg})
            continue
        try:
            m.db.execute("UPDATE files SET sha256_written=? WHERE id=?", (meta.sha256_file(f["current_path"]), f["id"]))
        except OSError:
            pass
        if c["field"] == "date":
            m.db.execute("UPDATE files SET date_taken=?, date_source='review', undated=0, updated_at=? WHERE id=?",
                         (value["date"], m.now(), f["id"]))
        else:
            m.db.execute("UPDATE files SET gps_lat=?, gps_lon=?, updated_at=? WHERE id=?",
                         (value["lat"], value["lon"], m.now(), f["id"]))
        m.db.execute("UPDATE conflicts SET status='resolved', chosen=?, resolved_at=? WHERE id=?",
                     (json.dumps(value), m.now(), c["id"]))
        left = m.db.execute("SELECT COUNT(*) FROM conflicts WHERE file_id=? AND status='pending'",
                            (f["id"],)).fetchone()[0]
        if left == 0 and f["status"] == "pending-conflict":
            m.db.execute("UPDATE files SET status='processed', updated_at=? WHERE id=?", (m.now(), f["id"]))
        m.event(None, f["id"], "conflicts", f"resolved-{c['field']}", json.dumps(value))
        m.db.commit()
        resolved += 1
    return {"ok": not failed, "resolved": resolved, "failed": failed, "skipped": skipped}


def write_choice(tools, f, field: str, value: dict) -> tuple[bool, str]:
    path = f["current_path"]
    if not path:
        return False, "the file has no current path"
    raw = meta.is_raw(path)
    if field == "date":
        tags = meta.date_tags(value["date"], value.get("offset"), raw)
    else:
        tags = meta.gps_tags(float(value["lat"]), float(value["lon"]), raw)
    try:
        return meta.safe_write(tools, path, tags, ident=f["pipeline_id"])
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def describe(c: dict) -> str:
    if "date" in c:
        return c["date"] + (c.get("offset") or "")
    return f"{c['lat']:.6f}, {c['lon']:.6f}"


def now_str() -> str:
    return datetime.now().strftime("%Y:%m:%d %H:%M:%S")
