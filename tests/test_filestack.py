"""Filestack layout (0.4): categories, the IMAGEPIPE_FILESTACK setting and `triage relocate`."""

from __future__ import annotations

from pathlib import Path

import pytest

from imagepipe.config import ConfigError, load_config
from imagepipe.triage import db as tdb
from imagepipe.triage.config import parse_categories, parse_triage
from imagepipe.triage.kinds import DEFAULT_CATEGORIES, classify

BASE = {"database": "/t/db", "log_dir": "/t/l", "reports_dir": "/t/r", "sources": [{"name": "s", "path": "/s"}]}


def _row(**kw):
    row = {"kind": "document", "ext": "", "mime": "", "catalogue_error": None, "rel_path": "x/a",
           "ext_mismatch": 0, "encrypted": 0, "takeout_title": None}
    row.update(kw)
    return row


def test_filestack_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("IMAGEPIPE_FILESTACK", "/fs")
    tc = parse_triage(dict(BASE))
    assert tc.filestack == Path("/fs") and tc.sorted_dir == Path("/fs/sorted")
    assert tc.work_dir == Path("/fs/triage/work") and tc.bucket_dir("quarantine/corrupt") == Path(
        "/fs/sorted/quarantine/corrupt")
    monkeypatch.delenv("IMAGEPIPE_FILESTACK")
    with pytest.raises(ConfigError, match="IMAGEPIPE_FILESTACK"):
        parse_triage(dict(BASE))


def test_default_categories():
    cats, overrides = parse_categories(None)
    assert list(cats)[:3] == ["images", "raw", "documents"] and overrides == {}
    assert cats["images"]["pipeline"] == "imagepipe" and cats["raw"]["pipeline"] == "imagepipe"
    assert cats["documents"]["pipeline"] == "none" and "pdf" in cats["documents"]["extensions"]
    assert "nef" in cats["raw"]["extensions"] and "psd" in cats["specials"]["extensions"]
    assert cats["raw-processed"] == {"pipeline": "none", "extensions": []}


def test_classify_uses_categories():
    assert classify(_row(kind="image", ext="jpg"), {}) == ("images", "image")
    assert classify(_row(kind="raw", ext="nef"), {}) == ("raw", "raw")
    assert classify(_row(kind="pdf", ext="pdf"), {}) == ("documents", "pdf")
    assert classify(_row(kind="video", ext="mp4"), {}) == ("video", "video")
    assert classify(_row(ext="txt"), {}) == ("documents", "document-txt")
    assert classify(_row(kind="empty", ext="jpg"), {})[0] == "quarantine/zero-byte"


def test_moving_an_extension_makes_an_override():
    raw = {k: dict(v) for k, v in DEFAULT_CATEGORIES.items()}
    raw["other-known"] = {"pipeline": "none", "extensions": [e for e in raw["other-known"]["extensions"] if e != "json"]}
    raw["documents"] = {"pipeline": "none", "extensions": raw["documents"]["extensions"] + ["json"]}
    raw["scans"] = {"pipeline": "none", "extensions": ["tifx"]}
    _cats, overrides = parse_categories(raw)
    assert overrides == {"json": "documents", "tifx": "scans"}
    assert classify(_row(ext="json"), overrides)[0] == "documents"
    assert classify(_row(kind="empty", ext="json"), overrides)[0] == "quarantine/zero-byte"  # content checks first


@pytest.mark.parametrize("raw, msg", [
    ({"images": {"extensions": ["jpg"]}, "photos": {"extensions": ["jpg"]}}, "both"),
    ({"quarantine": {"extensions": ["x"]}}, "can't be a category"),
    ({"Bad Name": {}}, "can't be a category"),
    ({"images": {"pipeline": "docupipe"}}, "pipeline must be one of"),
])
def test_bad_categories(raw, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_categories(raw)


def test_legacy_keys_still_load(monkeypatch):
    raw = dict(BASE, work_dir="/old/work", bucket_overrides={"bin": "docupipe"},
               destinations={"imagepipe": "/old/ip", "docupipe": "/old/dp", "sorted": "/old/s", "quarantine": "/old/q"})
    tc = parse_triage(raw)
    assert tc.legacy_destinations["imagepipe"] == Path("/old/ip") and tc.legacy_work_dir == Path("/old/work")
    assert tc.overrides == {"bin": "documents"}


def _put(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_relocate_moves_the_old_layout(tmp_path):
    from imagepipe.triage.relocate import run_relocate

    vault = tmp_path / "vault"
    old = {k: vault / k for k in ("imagepipe", "docupipe", "sorted", "quarantine")}
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(f"""
manifest: {tmp_path}/m.db
log_dir: {tmp_path}/l
reports_dir: {tmp_path}/r
triage:
  database: {tmp_path}/t/triage.db
  log_dir: {tmp_path}/t/logs
  reports_dir: {tmp_path}/t/reports
  work_dir: {vault}/triage-work
  sources: [{{name: photos, path: {vault}/photos}}]
  destinations: {{imagepipe: {old['imagepipe']}, docupipe: {old['docupipe']}, sorted: {old['sorted']}, quarantine: {old['quarantine']}}}
""")
    cfg = load_config(cfg_path)
    files = {
        "jpg": (_put(old["imagepipe"] / "photos" / "2014" / "IP1_a.jpg"), "image", None),
        "xmp": (_put(old["imagepipe"] / "photos" / "2014" / "IP1_a.xmp"), "sidecar", "jpg"),
        "nef": (_put(old["imagepipe"] / "photos" / "2014" / "IP2_b.NEF"), "raw", None),
        "nefxmp": (_put(old["imagepipe"] / "photos" / "2014" / "IP2_b.xmp"), "sidecar", "nef"),
        "pdf": (_put(old["docupipe"] / "file cabinet" / "IP3_c.pdf"), "pdf", None),
        "mp4": (_put(old["sorted"] / "video" / "photos" / "IP4_d.mp4"), "video", None),
        "bad": (_put(old["quarantine"] / "corrupt" / "photos" / "IP5_e.jpg"), "unknown", None),
    }
    untracked = _put(old["imagepipe"] / "photos" / "loose" / "x.CR2")
    (vault / "triage-work" / "empty").mkdir(parents=True)
    t = tdb.TriageDB(cfg.triage.database, cfg.tz)
    ids = {}
    for key, (path, kind, parent) in files.items():
        cur = t.db.execute(
            "INSERT INTO files (triage_id, source, rel_path, logical_path, dir, origin, current_path, size, mtime, "
            "kind, status, bucket, rule, companion_of) VALUES (?, 'photos', ?, ?, '', ?, ?, 1, 0, ?, 'done', 'x', 'r', ?)",
            (f"IP{key}", key, key, key, str(path), kind, ids.get(parent)))
        ids[key] = cur.lastrowid
    t.close()

    dry = run_relocate(cfg, dry_run=True)
    assert dry["would-move"] == 8 and files["jpg"][0].exists()

    res = run_relocate(cfg)
    assert res["moved"] == 8 and not res.get("error") and not res.get("copied")
    s = vault / "filestack" / "sorted"
    assert (s / "images" / "photos" / "2014" / "IP1_a.jpg").is_file()
    assert (s / "images" / "photos" / "2014" / "IP1_a.xmp").is_file()       # companion follows its photo
    assert (s / "raw" / "photos" / "2014" / "IP2_b.NEF").is_file()
    assert (s / "raw" / "photos" / "2014" / "IP2_b.xmp").is_file()          # companion follows its RAW
    assert (s / "documents" / "file cabinet" / "IP3_c.pdf").is_file()
    assert (s / "video" / "photos" / "IP4_d.mp4").is_file()
    assert (s / "quarantine" / "corrupt" / "photos" / "IP5_e.jpg").is_file()
    assert (s / "raw" / "photos" / "loose" / "x.CR2").is_file() and not untracked.exists()
    assert not (old["imagepipe"] / "photos").exists() and not (vault / "triage-work" / "empty").exists()

    t = tdb.TriageDB(cfg.triage.database, cfg.tz)
    nef = t.db.execute("SELECT * FROM files WHERE triage_id='IPnef'").fetchone()
    assert nef["current_path"] == str(s / "raw" / "photos" / "2014" / "IP2_b.NEF") and nef["bucket"] == "raw"
    q = t.db.execute("SELECT bucket FROM files WHERE triage_id='IPbad'").fetchone()[0]
    assert q == "quarantine/corrupt"
    assert t.db.execute("SELECT COUNT(*) FROM actions WHERE action='relocated'").fetchone()[0] == 7
    t.close()
    assert run_relocate(cfg) .get("moved", 0) == 0  # running it again finds nothing to do
