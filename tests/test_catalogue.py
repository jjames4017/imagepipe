import os
import sqlite3
import time

import pytest

from imagepipe.catalogue import CatalogueAbort, run_catalogue
from imagepipe.config import load_config
from imagepipe.reports import export_csv, write_reports
from tests.conftest import SAMPLES, write_config


def rows(cfg):
    db = sqlite3.connect(cfg.manifest)
    db.row_factory = sqlite3.Row
    out = {r["rel_path"]: dict(r) for r in db.execute("SELECT * FROM files")}
    db.close()
    return out


def test_first_run_catalogues_everything(cfg):
    result = run_catalogue(cfg, "test")
    assert result["status"] == "finished"
    assert result["scanned"] == len(SAMPLES)
    assert result["read"] == len(SAMPLES)
    r = rows(cfg)
    assert len(r) == len(SAMPLES)

    photo = r["2020/holiday/IMG_0001.jpg"]
    assert photo["kind"] == "image"
    assert (photo["width"], photo["height"]) == (64, 48)
    assert photo["has_exif"] == 1
    assert photo["camera_make"] == "TestCam"
    assert photo["camera_model"] == "Model X"
    assert photo["date_taken"] == "2020:01:02 03:04:05"
    assert photo["top_folder"] == "2020"
    assert len(photo["sha256"]) == 64
    assert photo["sha256"] == r["2020/holiday/IMG_0001 copy.jpg"]["sha256"]

    assert r["root_photo.jpg"]["top_folder"] == "(root)"
    assert r["broken/not_really.jpg"]["ext_mismatch"] == 1
    assert r["broken/empty.jpg"]["kind"] == "empty"
    assert r["2020/raw/DSC_0002.NEF"]["kind"] == "raw"


def test_second_run_reads_nothing(cfg):
    run_catalogue(cfg, "test")
    result = run_catalogue(cfg, "test")
    assert result["to_read"] == 0
    assert result["read"] == 0


def test_changed_and_new_files_are_reread(cfg, tree):
    run_catalogue(cfg, "test")
    target = tree / "docs/notes.txt"
    target.write_bytes(b"changed notes, now longer\n")
    future = time.time() + 10
    os.utime(target, (future, future))
    (tree / "new.png").write_bytes(SAMPLES["2020/holiday/screenshot.png"]())
    result = run_catalogue(cfg, "test")
    assert result["to_read"] == 2
    assert rows(cfg)["docs/notes.txt"]["size"] == len(b"changed notes, now longer\n")


def test_deleted_file_is_marked_missing_not_removed(cfg, tree):
    run_catalogue(cfg, "test")
    (tree / "mystery.bin").unlink()
    result = run_catalogue(cfg, "test")
    assert result["missing"] == 1
    assert rows(cfg)["mystery.bin"]["status"] == "missing"


def test_reappearing_file_is_restored(cfg, tree):
    run_catalogue(cfg, "test")
    data = (tree / "mystery.bin").read_bytes()
    (tree / "mystery.bin").unlink()
    run_catalogue(cfg, "test")
    (tree / "mystery.bin").write_bytes(data)
    run_catalogue(cfg, "test")
    assert rows(cfg)["mystery.bin"]["status"] == "catalogued"


def test_empty_source_with_known_files_aborts(cfg, tree):
    run_catalogue(cfg, "test")
    for p in sorted(tree.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    with pytest.raises(CatalogueAbort):
        run_catalogue(cfg, "test")
    assert all(r["status"] == "catalogued" for r in rows(cfg).values())


def test_source_is_never_modified(cfg, tree):
    before = {p: (p.stat().st_mtime, p.stat().st_size) for p in tree.rglob("*") if p.is_file()}
    run_catalogue(cfg, "test")
    after = {p: (p.stat().st_mtime, p.stat().st_size) for p in tree.rglob("*") if p.is_file()}
    assert before == after


def test_exclude_patterns(tmp_path, tree):
    cfgfile = write_config(tmp_path, tree)
    text = cfgfile.read_text().replace("host_path: /host/photos", "host_path: /host/photos\n    exclude: ['junk', '*.bin']")
    cfgfile.write_text(text)
    cfg = load_config(cfgfile)
    run_catalogue(cfg, "test")
    r = rows(cfg)
    assert "mystery.bin" not in r
    assert not any(k.startswith("junk/") for k in r)


def test_archive_listing(tmp_path, tree):
    cfg = load_config(write_config(tmp_path, tree, list_archive_contents=True))
    run_catalogue(cfg, "test")
    db = sqlite3.connect(cfg.manifest)
    names = {r[0]: r[1] for r in db.execute("SELECT name, kind_guess FROM archive_members")}
    assert names == {"a/photo1.jpg": "image", "a/readme.txt": "document"}


def test_reports_and_export(cfg):
    run_catalogue(cfg, "test")
    out = write_reports(cfg, cfg.source("test"))
    summary = (out / "summary.md").read_text()
    assert "Exact duplicate groups (same SHA-256): 1 groups, 2 files" in summary
    assert (out / "by_folder.csv").exists()
    mism = (out / "extension_mismatches.csv").read_text()
    assert "/host/photos/broken/not_really.jpg" in mism
    csv_path = export_csv(cfg, cfg.source("test"))
    lines = csv_path.read_text().splitlines()
    assert len(lines) == len(SAMPLES) + 1
    assert lines[1].startswith("/host/photos/")


def test_hash_none_then_sha256_fills_hashes(tmp_path, tree):
    cfg = load_config(write_config(tmp_path, tree, hash="none"))
    run_catalogue(cfg, "test")
    assert all(r["sha256"] is None for r in rows(cfg).values())
    cfg = load_config(write_config(tmp_path, tree, hash="sha256"))
    result = run_catalogue(cfg, "test")
    assert result["to_read"] == len(SAMPLES) - 1  # all but the empty file
    assert all(r["sha256"] for r in rows(cfg).values() if r["size"] > 0)
