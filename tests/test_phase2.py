"""Phase 2: batches, stage 1, stage 2 (IDs), stage 4 (exact duplicates), Takeout, conflicts, checks."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from PIL import Image

from imagepipe import ids
from imagepipe.config import load_config
from imagepipe.manifest import Manifest
from imagepipe.phase2 import conflicts as cf
from imagepipe.phase2 import meta
from imagepipe.phase2.checks import ready_check
from imagepipe.phase2.run import Phase2Error, run_phase2

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def jpeg(color=(200, 100, 50), size=(1200, 900), date="2020:01:02 03:04:05", model="Cam") -> bytes:
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x0110] = model
    if date:
        exif.get_ifd(0x8769)[0x9003] = date
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=90)
    return buf.getvalue()


def png(color=(1, 2, 3), size=(400, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_cfg(tmp_path, monkeypatch, enabled=True):
    stack = tmp_path / "filestack"
    monkeypatch.setenv("IMAGEPIPE_FILESTACK", str(stack))
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    data = tmp_path / "data"
    cfg = {"manifest": str(data / "manifest.db"), "log_dir": str(data / "logs"), "reports_dir": str(data / "reports"),
           "phase2": {"enabled": enabled, "batch_size": 50},
           "triage": {"database": str(data / "triage.db"), "log_dir": str(data / "tlogs"),
                      "reports_dir": str(data / "treports"), "sources": [{"name": "s", "path": str(src)}]}}
    path = tmp_path / "imagepipe.yml"
    path.write_text(yaml.safe_dump(cfg))
    return load_config(path), stack


def exif_set(path: Path, *args: str) -> None:
    subprocess.run(["exiftool", "-q", "-overwrite_original", *args, str(path)], check=True)


def test_disabled(tmp_path, monkeypatch):
    cfg, _ = make_cfg(tmp_path, monkeypatch, enabled=False)
    with pytest.raises(Phase2Error, match="switched off"):
        run_phase2(cfg)


def test_conflict_rules():
    local = {"key": "exif", "source": "camera", "date": "2020:01:02 14:00:00", "offset": None}
    utc = {"key": "takeout", "source": "Takeout", "date": "2020:01:02 04:00:00", "offset": "+00:00"}
    assert not cf.dates_agree(local, utc)
    sig, rec = cf.date_conflict([local, utc])
    assert sig == "date|exif|takeout|+10:00"
    assert rec["date"] == "2020:01:02 14:00:00" and rec["offset"] == "+10:00"
    same = dict(local, offset="+10:00")
    assert cf.dates_agree(same, utc)


@needs_exiftool
def test_phase2_batch(tmp_path, monkeypatch):
    cfg, stack = make_cfg(tmp_path, monkeypatch)
    images = stack / "sorted" / "images" / "holiday"
    images.mkdir(parents=True)
    (images / "a.jpg").write_bytes(jpeg())
    (images / "a copy.jpg").write_bytes(jpeg())                       # byte-identical
    (images / "b.jpg").write_bytes(jpeg(color=(10, 20, 30)))
    shutil.copy(images / "b.jpg", images / "b2.jpg")
    exif_set(images / "b2.jpg", "-XMP-dc:Subject=Beach", "-XMP:Rating=4")    # same pixels, more metadata
    exif_set(images / "b.jpg", "-XMP-dc:Description=Day one")
    (images / "c.jpg").write_bytes(png())                               # a PNG called .jpg, no date
    (images / "c.jpg.json").write_text(json.dumps({"title": "c.jpg", "photoTakenTime": {"timestamp": "1577934245"},
                                                   "geoData": {"latitude": -27.47, "longitude": 153.02}}))
    (images / "d.jpg").write_bytes(jpeg(color=(90, 90, 90), date="2020:01:05 14:00:00"))
    (images / "d.jpg.json").write_text(json.dumps({"title": "d.jpg", "photoTakenTime": {"timestamp": "1578196800"}}))
    (images / "small.jpg").write_bytes(jpeg(color=(5, 5, 5), size=(320, 240), date="2020:01:09 10:00:00"))
    (images / "broken.jpg").write_bytes(jpeg(color=(7, 7, 7), date="2020:01:10 10:00:00")[:600])

    res = run_phase2(cfg)
    assert res["status"] == "finished" and res["batch"] == "2020-01"
    m = Manifest(cfg.manifest, cfg.tz)
    rows = {Path(r["rel_path"]).name: r for r in m.db.execute("SELECT * FROM files")}
    # byte-identical: one kept, one excluded, both original paths recorded
    a, acopy = rows["a.jpg"], rows["a copy.jpg"]
    kept, gone = (a, acopy) if a["status"] == "processed" else (acopy, a)
    assert gone["status"] == "excluded" and gone["excluded_reason"].startswith("01-exact-dup")
    assert "/sorted/excluded/01-exact-dup/" in gone["current_path"] and gone["keeper_pipeline_id"] == kept["pipeline_id"]
    paths = {r[0] for r in m.db.execute("SELECT original_path FROM copies WHERE file_id=?", (kept["id"],))}
    assert len(paths) == 2
    # pixel-identical: metadata merged into the keeper (b2 has more tags), the other excluded
    b, b2 = rows["b.jpg"], rows["b2.jpg"]
    keeper = b if b["status"] == "processed" else b2
    other = b2 if keeper is b else b
    assert other["excluded_reason"].startswith("02-pixel-dup")
    tools = meta.Tools()
    md = meta.read_meta(tools.r, keeper["current_path"])
    assert "Beach" in md["keywords"] and md["rating"] == 4 and "Day one" in md["captions"]
    assert md["identifier"] == keeper["pipeline_id"]
    assert Path(keeper["current_path"]).name.startswith(keeper["pipeline_id"] + "_")
    assert "/imagepipe/processing/2020-01/images/holiday/" in keeper["current_path"]
    # c: extension corrected, date and GPS filled from Takeout
    c = rows["c.jpg"]
    assert c["status"] == "processed" and c["current_path"].endswith("_c.png")
    md = meta.read_meta(tools.r, c["current_path"])
    assert md["date"] == "2020:01:02 03:04:05" and md["gps"] and round(md["gps"][0], 2) == -27.47
    # d: camera time and Takeout disagree -> pending conflict, nothing written for the date
    d = rows["d.jpg"]
    assert d["status"] == "pending-conflict"
    con = m.db.execute("SELECT * FROM conflicts WHERE file_id=?", (d["id"],)).fetchone()
    assert con["field"] == "date" and con["status"] == "pending"
    # decode check and low-res flag
    assert rows["broken.jpg"]["excluded_reason"].startswith("10-corrupt")
    flags = {r[0] for r in m.db.execute("SELECT flag FROM flags WHERE file_id=?", (rows["small.jpg"]["id"],))}
    assert "low-res" in flags
    assert (cfg.reports_dir / "excluded.csv").read_text().count("\n") == 4  # header + 3

    # resolve the conflict with the recommendation (local time + zone)
    out = cf.resolve(m, tools, con["id"], "recommended")
    assert out["resolved"] == 1
    d = m.db.execute("SELECT * FROM files WHERE id=?", (d["id"],)).fetchone()
    assert d["status"] == "processed"
    raw = subprocess.run(["exiftool", "-s3", "-OffsetTimeOriginal", d["current_path"]], capture_output=True, text=True)
    assert raw.stdout.strip() == "+10:00"
    tools.close()
    m.close()

    rc = ready_check(cfg, "2020-01")
    assert rc["checked"] >= 4 and rc["not_ready"] >= 1  # c and d still have their Takeout .json beside them
    text = Path(rc["report"]).read_text()
    assert "stray sidecar c.jpg.json" in text

    # a second run: nothing new, no batch waiting
    res = run_phase2(cfg)
    assert res["batch"] is None


@needs_exiftool
def test_reentry(tmp_path, monkeypatch):
    cfg, stack = make_cfg(tmp_path, monkeypatch)
    images = stack / "sorted" / "images"
    images.mkdir(parents=True)
    (images / "x.jpg").write_bytes(jpeg(date="2021:03:01 10:00:00"))
    run_phase2(cfg)
    m = Manifest(cfg.manifest, cfg.tz)
    first = m.db.execute("SELECT * FROM files").fetchone()
    m.close()
    # the processed photo comes back in a later drop
    shutil.copy(first["current_path"], images / Path(first["current_path"]).name)
    res = run_phase2(cfg)
    assert res["stats"].get("excluded:01-exact-dup") == 1
    m = Manifest(cfg.manifest, cfg.tz)
    again = m.db.execute("SELECT * FROM files WHERE id != ?", (first["id"],)).fetchone()
    assert again["excluded_reason"] == "01-exact-dup: re-entry" and again["keeper_pipeline_id"] == first["pipeline_id"]
    assert ids.is_id(again["pipeline_id"]) and again["pipeline_id"] != first["pipeline_id"]
    m.close()


@needs_exiftool
def test_burst_and_raw_sidecar(tmp_path, monkeypatch):
    cfg, stack = make_cfg(tmp_path, monkeypatch)
    images = stack / "sorted" / "images"
    images.mkdir(parents=True)
    for i in range(3):
        (images / f"burst{i}.jpg").write_bytes(jpeg(color=(60 * i, 50, 50), date=f"2022:05:01 10:00:0{i}"))
    res = run_phase2(cfg)
    assert res["stats"].get("flag:burst") == 3


@needs_exiftool
def test_raw_gets_sidecar_not_written(tmp_path, monkeypatch):
    cfg, stack = make_cfg(tmp_path, monkeypatch)
    raw = stack / "sorted" / "raw"
    raw.mkdir(parents=True)
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (9, 9, 9)).save(buf, "TIFF")
    (raw / "IMG_1.dng").write_bytes(buf.getvalue())
    before = meta.sha256_file(raw / "IMG_1.dng")
    res = run_phase2(cfg)
    assert res["batch"] == "undated-001"
    m = Manifest(cfg.manifest, cfg.tz)
    r = m.db.execute("SELECT * FROM files").fetchone()
    m.close()
    p = Path(r["current_path"])
    assert r["status"] == "processed" and p.name == f"{r['pipeline_id']}_IMG_1.dng"
    assert meta.sha256_file(p) == before                     # the RAW itself is never written
    side = meta.sidecar_for(p)
    out = subprocess.run(["exiftool", "-s3", "-XMP-dc:Identifier", str(side)], capture_output=True, text=True)
    assert out.stdout.strip() == r["pipeline_id"]
