"""Triage: remove duplicates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from imagepipe.config import load_config
from imagepipe.fspath import to_db
from imagepipe.triage import db as tdb
from imagepipe.triage.config import DEFAULT_BACKUP_WORDS
from imagepipe.triage.dedupe import Scorer, run_dedupe, tokens


@pytest.fixture
def env(tmp_path, monkeypatch):
    stack = tmp_path / "filestack"
    monkeypatch.setenv("IMAGEPIPE_FILESTACK", str(stack))
    src = tmp_path / "src"
    src.mkdir()
    data = tmp_path / "data"
    cfg = {"manifest": str(data / "manifest.db"), "log_dir": str(data / "logs"), "reports_dir": str(data / "reports"),
           "tools": {"scansplitter": {"url": "http://127.0.0.1:9"}},
           "triage": {"database": str(data / "triage.db"), "log_dir": str(data / "tlogs"),
                      "reports_dir": str(data / "treports"), "sources": [{"name": "s", "path": str(src)}]}}
    path = tmp_path / "imagepipe.yml"
    path.write_text(yaml.safe_dump(cfg))
    c = load_config(path)
    t = tdb.TriageDB(c.triage.database, c.tz, None)
    n = {"i": 0}

    def add(category, logical, content=b"same", kind="document", ext="pdf", parent=0, name_date="2020-01-01",
            companion_of=None, sha=None, fname=None):
        n["i"] += 1
        dest = stack / "sorted" / category / Path(logical).parent / (fname or f"IP{n['i']:04d}_{Path(logical).name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        import hashlib

        cur = t.db.execute(
            "INSERT INTO files (triage_id, source, parent_id, rel_path, logical_path, dir, origin, current_path, size, "
            "mtime, ext, kind, sha256, status, bucket, companion_of, name_date, dest_path) "
            "VALUES (?, 's', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?, ?, ?)",
            (f"IP{n['i']:04d}", parent, logical, logical, str(Path(logical).parent), f"s/{logical}", str(dest),
             len(content), dest.stat().st_mtime, ext, kind, sha or hashlib.sha256(content).hexdigest(), category,
             companion_of, name_date, str(dest)))
        t.db.commit()
        return cur.lastrowid, dest

    yield c, t, add, stack
    t.close()


def test_scorer():
    s = Scorer(DEFAULT_BACKUP_WORDS, ["Stuff2"])
    assert tokens("PhotoTemp") == ["photo", "temp"] and tokens("drive_backup") == ["drive", "backup"]
    assert s.folder("Whakapapa") == 1
    assert s.folder("drive_backup") == -2 and s.folder("PhotoTemp") == -2 and s.folder("Old Photos") == -2
    assert s.folder("Golden Bay") == 1          # 'old' only as a whole word
    assert s.folder("DCIM") == 0 and s.folder("New folder") == 0 and s.folder("Stuff2") == 0


def test_keeper_by_folder_then_oldest(env):
    c, t, add, stack = env
    k, kpath = add("documents", "file cabinet/Whakapapa/chart.pdf")
    d1, d1path = add("documents", "file cabinet/drive_backup/PhotoTemp/chart.pdf")
    d2, d2path = add("documents", "file cabinet/Whakapapa/chart.pdf", parent=5)          # from an archive
    o1, o1path = add("documents", "a/Tax/x.pdf", content=b"other", name_date="2019-05-01")
    o2, o2path = add("documents", "b/Tax/x.pdf", content=b"other", name_date="2018-05-01")  # older: kept

    dry = run_dedupe(c, dry_run=True)
    assert dry["stats"] == {"excluded": 3} and d1path.exists()
    res = run_dedupe(c)
    assert res["stats"]["excluded"] == 3
    assert kpath.exists() and o2path.exists()
    dup_dir = stack / "sorted/excluded/triage-duplicate"
    assert {p.name for p in dup_dir.iterdir()} == {d1path.name, d2path.name, o1path.name}
    t.db.commit()
    rows = t.db.execute("SELECT file_id, keeper_id FROM duplicates ORDER BY file_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(d1, k), (d2, k), (o1, o2)]
    assert run_dedupe(c)["stats"] == {}   # nothing left


def test_image_sidecars(env):
    c, t, add, stack = env
    k, kpath = add("images", "Bali/IMG_1.jpg", content=b"jpg1", kind="image", ext="jpg")
    d, dpath = add("images", "backup/IMG_1.jpg", content=b"jpg1", kind="image", ext="jpg")
    s, spath = add("images", "backup/IMG_1.jpg.json", content=b"{}", kind="document", ext="json", companion_of=d,
                   fname=dpath.name + ".json")   # companions take their photo's name
    # both have sidecars: passed through for stage 4
    k2, k2path = add("images", "Bali/IMG_2.jpg", content=b"jpg2", kind="image", ext="jpg")
    add("images", "Bali/IMG_2.json", content=b"{1}", kind="document", ext="json", companion_of=k2)
    d2, d2path = add("images", "backup/IMG_2.jpg", content=b"jpg2", kind="image", ext="jpg")
    add("images", "backup/IMG_2.json", content=b"{2}", kind="document", ext="json", companion_of=d2)
    res = run_dedupe(c)
    assert res["stats"]["excluded"] == 1 and res["stats"]["passed-to-stage-4"] == 1
    assert res["stats"]["companions-to-keeper"] == 1
    moved = kpath.with_name(kpath.stem + ".jpg.json")
    assert moved.exists() and not spath.exists()
    assert t.db.execute("SELECT companion_of FROM files WHERE id=?", (s,)).fetchone()[0] == k
    assert k2path.exists() and d2path.exists()


def test_split_pdf_wins_and_waiting_pages_removed(env, monkeypatch):
    c, t, add, stack = env
    best, bpath = add("documents", "Whakapapa/album.pdf")
    split, spath = add("documents", "drive_backup/album.pdf")
    waiting, wpath = add("documents", "Family/album.pdf")
    t.db.execute("INSERT INTO pdf_checks (path, verdict, state, final_path) VALUES (?, 'photo-pages', 'done', ?)",
                 (to_db(str(spath)), to_db(str(spath))))
    t.db.execute("INSERT INTO pdf_checks (path, verdict, state, ss_project, ss_scans, final_path) "
                 "VALUES (?, 'photo-pages', 'waiting-review', 'p1', ?, ?)",
                 (to_db(str(wpath)), json.dumps([{"sid": "a", "page": 1, "exported": False}]), to_db(str(wpath))))
    t.db.commit()
    removed = []
    from imagepipe.scansplitter import ScanSplitter

    monkeypatch.setattr(ScanSplitter, "delete_scan", lambda self, pid, sid: removed.append((pid, sid)))
    res = run_dedupe(c)
    t.db.commit()
    keeper = t.db.execute("SELECT DISTINCT keeper_id FROM duplicates").fetchone()[0]
    assert keeper == split                              # already split beats waiting beats the best folder
    assert spath.exists() and not wpath.exists() and not bpath.exists()
    assert removed == [("p1", "a")]                     # the waiting copy's page left ScanSplitter
    assert res["stats"]["excluded"] == 2


def test_waiting_duplicate_pages_removed(env, monkeypatch):
    c, t, add, stack = env
    k, kpath = add("documents", "Whakapapa/album.pdf")
    d, dpath = add("documents", "Old/album.pdf")
    for p, proj in ((kpath, "p1"), (dpath, "p2")):
        t.db.execute("INSERT INTO pdf_checks (path, verdict, state, ss_project, ss_scans, final_path) "
                     "VALUES (?, 'photo-pages', 'waiting-review', ?, ?, ?)",
                     (to_db(str(p)), proj, json.dumps([{"sid": "s-" + proj, "page": 1, "exported": False}]),
                      to_db(str(p))))
    t.db.commit()
    removed = []
    from imagepipe.scansplitter import ScanSplitter

    monkeypatch.setattr(ScanSplitter, "delete_scan", lambda self, pid, sid: removed.append((pid, sid)))
    res = run_dedupe(c)
    assert removed == [("p2", "s-p2")] and res["stats"]["scansplitter-pages-removed"] == 1
    t.db.commit()
    row = t.db.execute("SELECT verdict, state FROM pdf_checks WHERE path=?", (to_db(str(dpath)),)).fetchone()
    assert tuple(row) == ("duplicate", "done")
