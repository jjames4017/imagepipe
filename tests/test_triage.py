"""Triage: scan -> plan -> apply on a generated tree. No personal files."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import zipfile
import tarfile
from pathlib import Path

import pytest
from PIL import Image

from imagepipe import exiftool as exiftool_mod
from imagepipe.config import ConfigError, load_config
from imagepipe.ids import ID_PATTERN, id_from_name, is_id, new_id
from imagepipe.triage.apply import run_apply
from imagepipe.triage.naming import date_in_text, date_from_path, descriptive_filename, meaningful_folder, smartname
from imagepipe.triage.plan import run_plan, takeout_candidates
from imagepipe.triage.scan import run_scan

HAS_EXIFTOOL = exiftool_mod.available()
try:
    import reverse_geocoder
    del reverse_geocoder
    HAS_GEO = True
except Exception:
    HAS_GEO = False


def jpeg(date=None, gps=None, color=(200, 100, 50)) -> bytes:
    im = Image.new("RGB", (40, 30), color)
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    if date:
        exif.get_ifd(0x8769)[0x9003] = date
    if gps:
        lat, lon = gps
        g = exif.get_ifd(0x8825)
        g[1] = "S" if lat < 0 else "N"
        g[2] = (abs(lat), 0.0, 0.0)
        g[3] = "W" if lon < 0 else "E"
        g[4] = (abs(lon), 0.0, 0.0)
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def zip_of(members: dict[str, bytes], encrypt_flag=False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=(2010, 5, 6, 7, 8, 9)), data)
    raw = bytearray(buf.getvalue())
    if encrypt_flag:  # mark every entry as password protected (local and central headers)
        for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            i = raw.find(sig)
            while i != -1:
                raw[i + off] |= 0x1
                i = raw.find(sig, i + 4)
    return bytes(raw)


def tgz_of(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = 1300000000
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def docx(title: str, created: str) -> bytes:
    core = (f'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
            f'<dc:title>{title}</dc:title><dcterms:created>{created}</dcterms:created></cp:coreProperties>')
    return zip_of({"[Content_Types].xml": b"<Types/>", "docProps/core.xml": core.encode(), "word/document.xml": b"<w/>"})


BALI = jpeg(date="2014:07:12 10:20:30", gps=(-8.65, 115.2167))

PHOTOS = {
    "2014/Bali/IMG_1234.jpg": BALI,
    "2014/Bali/IMG_1234.xmp": b"<?xpacket begin=''?><x:xmpmeta xmlns:x='adobe:ns:meta/'/>",
    "old phone backup/DCIM/IMG_1234.jpg": BALI,                         # exact duplicate - both carry on
    "2019/Mum 70th/IMG_5555.jpg": jpeg(color=(1, 2, 3)),
    "2019/Mum 70th/IMG_5555.MOV": b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 100,
    "Takeout/Google Photos/Trip to Hoi An/PXL_20221003_101010123.jpg": jpeg(color=(9, 9, 9)),
    "Takeout/Google Photos/Trip to Hoi An/PXL_20221003_101010123.jpg.supplemental-metad.json": json.dumps({
        "title": "PXL_20221003_101010123.jpg", "photoTakenTime": {"timestamp": "1664791810"},
        "geoData": {"latitude": 15.8794, "longitude": 108.335}}).encode(),
    "junk/.DS_Store": b"\x00\x00\x00\x01Bud1" + b"\x00" * 50,
    "junk/empty.jpg": b"",
    "junk/not_really.jpg": b"<!DOCTYPE html><html><body>404</body></html>",
    "misc/mystery.qqq": bytes(range(256)) * 2,
    "misc/clip.mp4": b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 100,
    "misc/song.mp3": b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100,
    "misc/setup.exe": b"MZ\x90\x00" + b"\x00" * 200,
    "misc/app.ts": b"const x: number = 1;\n",
}
CABINET = {
    "Tax/2019/receipt.pdf": b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 100,
    "letters/letter.docx": docx("Letter to the Council", "2016-03-04T05:06:07Z"),
    "letters/notes.txt": b"just some notes\n",
    "Holidays/Fiji 2010.zip": zip_of({
        "day1/photo1.jpg": jpeg(color=(5, 6, 7)),
        "itinerary.pdf": b"%PDF-1.4\n" + b"1" * 80,
        "inner.zip": zip_of({"a.txt": b"inside inner\n"}),
    }),
    "Holidays/locked.zip": zip_of({"secret.jpg": b"\xff\xd8\xff" + b"0" * 50}, encrypt_flag=True),
    "Holidays/broken.zip": b"PK\x03\x04" + b"garbage" * 20,
    "Holidays/backup.tar.gz": tgz_of("old/notes2.txt", b"tar notes\n"),
}


def build(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


@pytest.fixture
def env(tmp_path):
    vault = tmp_path / "vault"
    build(vault / "photos", PHOTOS)
    build(vault / "file cabinet", CABINET)
    cfg_path = tmp_path / "imagepipe.yml"
    cfg_path.write_text(f"""
timezone: Australia/Brisbane
manifest: {tmp_path}/data/imagepipe.db
log_dir: {tmp_path}/data/logs
reports_dir: {tmp_path}/data/reports
sources: []
triage:
  database: {tmp_path}/triage/triage.db
  log_dir: {tmp_path}/triage/logs
  reports_dir: {tmp_path}/triage/reports
  work_dir: {vault}/triage-work
  sources:
    - name: photos
      path: {vault}/photos
    - name: file-cabinet
      path: "{vault}/file cabinet"
  destinations:
    imagepipe: {vault}/imagepipe
    docupipe: {vault}/docupipe
    sorted: {vault}/sorted
    quarantine: {vault}/quarantine
  workers: 2
  embed_metadata: [image, pdf]
""")
    cfg = load_config(cfg_path)
    return cfg, vault


def rows(cfg) -> dict[str, dict]:
    db = sqlite3.connect(cfg.triage.database)
    db.row_factory = sqlite3.Row
    out = {f"{r['source']}:{r['logical_path']}": dict(r) for r in db.execute("SELECT * FROM files")}
    db.close()
    return out


def scan_all(cfg):
    for s in cfg.triage.sources:
        assert run_scan(cfg, s)["status"] == "finished"


# ---------------------------------------------------------------- unit tests
def test_ids():
    a, b = new_id(), new_id()
    assert a != b and is_id(a) and len(a) == 18 and a < b
    assert id_from_name(f"{a}_2014-07-12_Bali.jpg") == a
    assert id_from_name("IMG_1234.jpg") is None


def test_dates_in_names():
    assert date_in_text("IMG_20140712_101010") == "2014-07-12"
    assert date_in_text("Screenshot 2019-05-01 at 10.00") == "2019-05-01"
    assert date_in_text("party 25-12-2018") == "2018-12-25"
    assert date_in_text("July 2014") == "2014-07"
    assert date_in_text("Bali 2014") == "2014"
    assert date_in_text("IMG_1234") is None
    assert date_from_path("IMG_1234.jpg", ["2014", "07"]) == ("2014-07", "folder")


def test_smartname_parts():
    assert meaningful_folder("DCIM") is None
    assert meaningful_folder("100APPLE") is None
    assert meaningful_folder("2014 Bali") == "Bali"
    assert descriptive_filename("IMG_1234") is None
    assert descriptive_filename("Grandma at the beach") == "Grandma at the beach"
    name, src = smartname("Hoi An VN", None, ["Takeout", "Google Photos", "Trip to Hoi An"], "PXL_1")
    assert name == "Hoi-An-VN-Trip-to-Hoi-An" and src == "gps+folder"
    assert smartname(None, None, ["DCIM"], "IMG_1234") == ("IMG-1234", "original-name")
    assert "." not in smartname(None, "St. Kilda pier", [], "x")[0]


def test_takeout_candidates():
    c = takeout_candidates("IMG_1.jpg.supplemental-metad.json", None)
    assert "IMG_1.jpg" in c
    assert "IMG_1(1).jpg" in takeout_candidates("IMG_1.jpg(1).json", None)


def test_embed_is_off_by_default(env):
    cfg, vault = env
    from imagepipe.triage.config import parse_triage

    raw = {"database": "/t/db", "log_dir": "/t/l", "reports_dir": "/t/r", "work_dir": "/w",
           "sources": [{"name": "s", "path": "/s"}],
           "destinations": {"imagepipe": "/a", "docupipe": "/b", "sorted": "/c", "quarantine": "/d"}}
    assert parse_triage(raw).embed_metadata == []


def test_config_rejects_overlap(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text(f"""
manifest: {tmp_path}/m.db
log_dir: {tmp_path}/l
reports_dir: {tmp_path}/r
triage:
  database: {tmp_path}/t.db
  log_dir: {tmp_path}/tl
  reports_dir: {tmp_path}/tr
  work_dir: {tmp_path}/src/work
  sources: [{{name: s, path: {tmp_path}/src}}]
  destinations: {{imagepipe: {tmp_path}/a, docupipe: {tmp_path}/b, sorted: {tmp_path}/c, quarantine: {tmp_path}/d}}
""")
    with pytest.raises(ConfigError, match="overlap"):
        load_config(p)


# ---------------------------------------------------------------- end to end
def test_plan_moves_nothing_and_names_files(env):
    cfg, vault = env
    scan_all(cfg)
    result = run_plan(cfg)
    assert result["status"] == "finished"
    assert (Path(result["report"]) / "plan.csv").is_file()
    for rel in PHOTOS:
        assert (vault / "photos" / rel).exists()
    assert not (vault / "imagepipe").exists()

    r = rows(cfg)
    bali = r["photos:2014/Bali/IMG_1234.jpg"]
    assert bali["bucket"] == "imagepipe" and bali["date_source"] == "exif"
    dest = Path(bali["dest_path"])
    assert dest.parent == vault / "imagepipe" / "photos" / "2014" / "Bali"
    assert re.fullmatch(rf"{ID_PATTERN}_2014-07-12_.+\.jpg", dest.name), dest.name
    assert dest.name.startswith(bali["triage_id"])
    if HAS_GEO:
        assert bali["smartname_source"] == "gps+folder" and "-ID-" in bali["smartname"]
    xmp = r["photos:2014/Bali/IMG_1234.xmp"]
    assert xmp["companion_of"] == bali["id"] and xmp["dest_path"] == str(dest.with_suffix(".xmp"))

    dup = r["photos:old phone backup/DCIM/IMG_1234.jpg"]
    assert dup["bucket"] == "imagepipe" and dup["sha256"] == bali["sha256"]
    assert dup["dest_path"].startswith(str(vault / "imagepipe" / "photos" / "old phone backup" / "DCIM"))

    live = r["photos:2019/Mum 70th/IMG_5555.MOV"]
    still = r["photos:2019/Mum 70th/IMG_5555.jpg"]
    assert live["rule"] == "live-photo-clip" and live["bucket"] == "imagepipe"
    assert Path(live["dest_path"]).stem == Path(still["dest_path"]).stem
    assert "Mum-70th" in still["smartname"]

    pxl = r["photos:Takeout/Google Photos/Trip to Hoi An/PXL_20221003_101010123.jpg"]
    tj = r["photos:Takeout/Google Photos/Trip to Hoi An/PXL_20221003_101010123.jpg.supplemental-metad.json"]
    assert tj["companion_of"] == pxl["id"] and tj["dest_path"] == pxl["dest_path"] + ".json"
    assert pxl["date_source"] == "sidecar-takeout" and pxl["name_date"] == "2022-10-03"
    if HAS_GEO:
        assert pxl["smartname"].startswith("Hoi-An-VN")

    assert r["photos:junk/.DS_Store"]["bucket"] == "quarantine/system-junk"
    assert r["photos:junk/empty.jpg"]["bucket"] == "quarantine/zero-byte"
    assert r["photos:junk/not_really.jpg"]["bucket"] == "quarantine/corrupt"
    assert r["photos:misc/mystery.qqq"]["bucket"] == "quarantine/unknown-type"
    assert r["photos:misc/clip.mp4"]["bucket"] == "sorted/video"
    assert r["photos:misc/song.mp3"]["bucket"] == "sorted/audio"
    assert r["photos:misc/setup.exe"]["bucket"] == "sorted/software"
    assert r["photos:misc/app.ts"]["bucket"] == "sorted/code"
    assert r["photos:misc/clip.mp4"]["dest_path"].startswith(str(vault / "sorted" / "video" / "photos" / "misc"))

    letter = r["file-cabinet:letters/letter.docx"]
    assert letter["bucket"] == "docupipe" and letter["name_date"] == "2016-03-04"
    assert letter["smartname"] == "Letter-to-the-Council"
    assert Path(letter["dest_path"]).parent == vault / "docupipe" / "file cabinet" / "letters"
    receipt = r["file-cabinet:Tax/2019/receipt.pdf"]
    assert receipt["bucket"] == "docupipe" and receipt["smartname"] == "Tax-receipt"
    assert receipt["name_date"] == "2019" and receipt["date_source"] == "folder"
    assert r["file-cabinet:Holidays/Fiji 2010.zip"]["bucket"] == "extract"


def test_apply_end_to_end(env):
    cfg, vault = env
    scan_all(cfg)
    run_plan(cfg)
    result = run_apply(cfg)
    assert result["status"] == "finished", result
    stats = result["stats"]
    assert stats.get("errors", 0) == 0, stats

    # sources are empty of files (folders are left alone)
    left = [p for p in (vault / "photos").rglob("*") if p.is_file()]
    left += [p for p in (vault / "file cabinet").rglob("*") if p.is_file()]
    assert left == []

    r = rows(cfg)
    for key, row in r.items():
        if row["bucket"] != "extract":
            assert row["status"] == "done", (key, row["status"])
            assert Path(row["current_path"]).is_file(), key

    # archive contents: extracted under a folder named for the archive, archive deleted
    fiji = r["file-cabinet:Holidays/Fiji 2010.zip"]
    assert fiji["status"] == "extracted"
    photo = r["file-cabinet:Holidays/Fiji 2010/day1/photo1.jpg"]
    assert photo["bucket"] == "imagepipe" and photo["origin"].endswith("Fiji 2010.zip::day1/photo1.jpg")
    assert Path(photo["current_path"]).parent == vault / "imagepipe" / "file cabinet" / "Holidays" / "Fiji 2010" / "day1"
    assert r["file-cabinet:Holidays/Fiji 2010/itinerary.pdf"]["bucket"] == "docupipe"
    inner = r["file-cabinet:Holidays/Fiji 2010/inner/a.txt"]
    assert inner["status"] == "done" and inner["bucket"] == "docupipe"
    assert r["file-cabinet:Holidays/backup/old/notes2.txt"]["status"] == "done"

    locked = r["file-cabinet:Holidays/locked.zip"]
    assert locked["bucket"] == "quarantine/encrypted" and Path(locked["current_path"]).is_file()
    assert Path(locked["current_path"]).parent == vault / "quarantine" / "encrypted" / "file cabinet" / "Holidays"
    assert r["file-cabinet:Holidays/broken.zip"]["bucket"] == "quarantine/archive-failed"

    # the audit trail: every file has a 'moved' action; archives have 'archive-deleted'
    with open(cfg.triage.reports_dir / "actions.csv", encoding="utf-8") as fh:
        actions = list(csv.DictReader(fh))
    moved = {a["triage_id"] for a in actions if a["action"] == "moved"}
    for row in r.values():
        if row["status"] == "done":
            assert row["triage_id"] in moved
    assert any(a["action"] == "archive-deleted" and a["triage_id"] == fiji["triage_id"] for a in actions)
    assert not any((vault / "triage-work").rglob("*.*"))

    if HAS_EXIFTOOL:
        bali = r["photos:2014/Bali/IMG_1234.jpg"]
        assert bali["embed_status"] == "done" and bali["sha256_final"] != bali["sha256"]
        et = exiftool_mod.ExifTool()
        tags = et.read(bali["current_path"], ("-XMP-dc:Identifier", "-XMP-dc:Source"))
        et.close()
        assert tags.get("Identifier") == bali["triage_id"]
        assert tags.get("Source", "").endswith("photos/2014/Bali/IMG_1234.jpg")

    # a second scan finds nothing to do and is not alarmed by the empty sources
    for s in cfg.triage.sources:
        res = run_scan(cfg, s)
        assert res["status"] == "finished" and res["to_read"] == 0


def test_changed_file_is_not_moved(env):
    cfg, vault = env
    scan_all(cfg)
    run_plan(cfg)
    target = vault / "file cabinet" / "letters" / "notes.txt"
    target.write_bytes(b"changed after the plan\n")
    run_apply(cfg)
    assert target.exists()
    assert rows(cfg)["file-cabinet:letters/notes.txt"]["status"] == "changed"


def test_limit_and_recovery(env):
    cfg, vault = env
    scan_all(cfg)
    run_plan(cfg)
    result = run_apply(cfg, limit=3)
    assert sum(v for k, v in result["stats"].items() if k.startswith("moved:")) == 3

    # simulate a run stopped half way through a move: link made, original not yet removed
    db = sqlite3.connect(cfg.triage.database)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM files WHERE status='planned' AND bucket='docupipe' LIMIT 1").fetchone()
    dest = Path(row["dest_path"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.link(row["current_path"], dest)
    db.execute("UPDATE files SET status='moving' WHERE id=?", (row["id"],))
    db.commit()
    db.close()

    result = run_apply(cfg)
    assert result["status"] == "finished"
    r = {x["id"]: x for x in rows(cfg).values()}
    assert r[row["id"]]["status"] == "done"
    assert not os.path.exists(row["current_path"]) and dest.is_file()


# ---------------------------------------------------------------- odd names and failures
def test_names_that_are_not_utf8(env):
    cfg, vault = env
    folder = os.path.join(os.fsencode(vault / "photos"), b"caf\xe9 trip")
    os.makedirs(folder)
    with open(os.path.join(folder, b"IMG_0042.jpg"), "wb") as fh:
        fh.write(jpeg(color=(4, 4, 4)))
    with open(os.path.join(os.fsencode(vault / "file cabinet"), b"r\xe9sum\xe9.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4\n" + b"2" * 80)

    scan_all(cfg)
    run_plan(cfg)
    result = run_apply(cfg)
    assert result["status"] == "finished" and result["stats"].get("errors", 0) == 0, result

    r = rows(cfg)
    photo = next(v for k, v in r.items() if k.endswith("IMG_0042.jpg"))
    assert photo["status"] == "done"
    # the original folder name's bytes are kept in the destination; the new file name is clean UTF-8
    dest_dir = os.path.join(os.fsencode(vault / "imagepipe" / "photos"), b"caf\xe9 trip")
    moved = os.listdir(dest_dir)
    assert len(moved) == 1 and moved[0].endswith(b"_caf-trip.jpg"), moved
    moved[0].decode("utf-8")  # valid UTF-8
    pdf = next(v for k, v in r.items() if k.endswith(".pdf") and "sum" in k)
    assert pdf["status"] == "done" and os.path.exists(os.fsencode(pdf["current_path"].replace("", "\udce9")))


def test_one_bad_file_does_not_stop_scan(env, monkeypatch):
    import imagepipe.triage.scan as scan_mod

    cfg, vault = env
    real = scan_mod._write_row

    def flaky(t, r, info, run_id):
        if info["rel_path"].endswith("song.mp3"):
            raise RuntimeError("simulated failure")
        return real(t, r, info, run_id)

    monkeypatch.setattr(scan_mod, "_write_row", flaky)
    res = run_scan(cfg, cfg.triage.source("photos"))
    assert res["status"] == "finished" and res["read_problems"] >= 1
    r = rows(cfg)
    assert "photos:misc/song.mp3" not in r and "photos:misc/clip.mp4" in r


def test_one_bad_move_or_archive_does_not_stop_apply(env, monkeypatch):
    import imagepipe.triage.apply as apply_mod

    cfg, vault = env
    scan_all(cfg)
    run_plan(cfg)
    real_transfer, real_extract = apply_mod.transfer, apply_mod.extract

    def bad_transfer(src, dest, sha, verify):
        if src.endswith("notes.txt"):
            raise RuntimeError("simulated move failure")
        return real_transfer(src, dest, sha, verify)

    def bad_extract(path, mime, dest, max_bytes, progress=None):
        if path.endswith("Fiji 2010.zip"):
            raise RuntimeError("simulated extraction crash")
        return real_extract(path, mime, dest, max_bytes, progress)

    monkeypatch.setattr(apply_mod, "transfer", bad_transfer)
    monkeypatch.setattr(apply_mod, "extract", bad_extract)
    result = run_apply(cfg)
    assert result["status"] == "finished"
    r = rows(cfg)
    assert r["file-cabinet:letters/notes.txt"]["status"] == "error"
    assert (vault / "file cabinet" / "letters" / "notes.txt").exists()
    fiji = r["file-cabinet:Holidays/Fiji 2010.zip"]
    assert fiji["status"] == "error" and (vault / "file cabinet" / "Holidays" / "Fiji 2010.zip").exists()
    assert r["file-cabinet:letters/letter.docx"]["status"] == "done"
    assert r["photos:2014/Bali/IMG_1234.jpg"]["status"] == "done"
    assert not any((vault / "triage-work").glob("*/*"))


# ------------------------------------------------- PSD, OLE Office files, problem count
def psd(width=40, height=30, version=1) -> bytes:
    """A minimal Photoshop header Pillow can't open (no image data)."""
    return (b"8BPS" + version.to_bytes(2, "big") + b"\x00" * 6 + (3).to_bytes(2, "big")
            + height.to_bytes(4, "big") + width.to_bytes(4, "big") + (8).to_bytes(2, "big")
            + (3).to_bytes(2, "big") + b"\x00" * 64)


def ole(streams: list[str]) -> bytes:
    """A minimal OLE compound file: one FAT sector, one directory sector."""
    import struct

    end, free = 0xFFFFFFFE, 0xFFFFFFFF
    hdr = struct.pack("<8s16sHHHHH6sIIIIIIIII", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"\x00" * 16,
                      0x3E, 3, 0xFFFE, 9, 6, b"\x00" * 6, 0, 1, 1, 0, 4096, end, 0, end, 0)
    hdr += struct.pack("<109I", 0, *([free] * 108))
    fat = struct.pack("<128I", 0xFFFFFFFD, end, *([free] * 126))

    def entry(name: str, otype: int) -> bytes:
        raw = (name + "\x00").encode("utf-16-le")
        return (raw.ljust(64, b"\x00") + struct.pack("<HBB3I", len(raw), otype, 1, free, free, free)
                + b"\x00" * 36 + struct.pack("<IQ", end, 0))

    entries = entry("Root Entry", 5) + b"".join(entry(s, 2) for s in streams)
    return hdr + fat + entries.ljust(512, b"\x00")


def test_psd_and_ole_readers(tmp_path):
    from imagepipe.triage.meta import ole_office_meta, ole_stream_names, psd_meta

    p = tmp_path / "a.psd"
    p.write_bytes(psd(1200, 800))
    assert psd_meta(str(p)) == {"width": 1200, "height": 800}
    p.write_bytes(psd(5, 6, version=2))  # PSB
    assert psd_meta(str(p)) == {"width": 5, "height": 6}
    p.write_bytes(b"8BPS" + b"\x00" * 4)
    with pytest.raises(ValueError):
        psd_meta(str(p))

    o = tmp_path / "x.xlsx"
    o.write_bytes(ole(["EncryptionInfo", "EncryptedPackage"]))
    assert ole_stream_names(str(o)) >= {"Root Entry", "EncryptionInfo", "EncryptedPackage"}
    assert ole_office_meta(str(o)) == {"encrypted": 1}
    o.write_bytes(ole(["Workbook"]))
    assert ole_office_meta(str(o)) == {}
    o.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    with pytest.raises(ValueError):
        ole_stream_names(str(o))


def test_psd_and_protected_office_are_not_problems(env):
    cfg, vault = env
    build(vault / "file cabinet", {
        "art/war.PSD": psd(1200, 800),
        "fin/Banking Details.docx": ole(["EncryptionInfo", "EncryptedPackage", "\x06DataSpaces"]),
        "fin/old format.xlsx": ole(["Workbook", "\x05SummaryInformation"]),
        "fin/really html.xlsx": b"<html><body><table></table></body></html>",
        "fin/damaged.docx": b"PK\x03\x04" + b"garbage" * 20,
    })
    res = run_scan(cfg, cfg.triage.source("file-cabinet"))
    assert res["status"] == "finished"
    r = rows(cfg)
    war = r["file-cabinet:art/war.PSD"]
    assert war["catalogue_error"] is None and (war["width"], war["height"]) == (1200, 800)
    locked = r["file-cabinet:fin/Banking Details.docx"]
    assert locked["catalogue_error"] is None and locked["encrypted"] == 1
    for name in ("old format.xlsx", "really html.xlsx"):
        assert r[f"file-cabinet:fin/{name}"]["catalogue_error"] is None, name
    assert "BadZipFile" in r["file-cabinet:fin/damaged.docx"]["catalogue_error"]  # real damage still reported
    assert res["read_problems"] == 1

    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    assert r["file-cabinet:art/war.PSD"]["bucket"] == "imagepipe"
    assert r["file-cabinet:fin/Banking Details.docx"]["bucket"] == "quarantine/encrypted"
    assert r["file-cabinet:fin/old format.xlsx"]["bucket"] == "docupipe"


def test_progress_problem_count_is_a_running_total(monkeypatch):
    import imagepipe.triage.scan as scan_mod

    class Pool:
        def imap_unordered(self, fn, items, chunksize=1):
            return iter([{"key": 1, "size": 1, "catalogue_error": "x"}, {"key": 2, "size": 1, "catalogue_error": None}])

    class Prog:
        extra = ""

        def update(self, n, nbytes, extra=None):
            self.extra = extra

    monkeypatch.setattr(scan_mod, "_write_rows", lambda *a: 0)
    p = Prog()
    assert scan_mod.read_items(None, Pool(), [], {}, 1, p, errors_before=30) == 1
    assert p.extra == "31 problems"


# ------------------------------------------------- re-read problem files / everything
def test_reread_problems_and_all(env):
    import imagepipe.triage.scan as scan_mod
    from imagepipe.cli import build_parser

    cfg, vault = env
    src = cfg.triage.source("file-cabinet")
    assert run_scan(cfg, src)["status"] == "finished"
    db = sqlite3.connect(cfg.triage.database)
    # pretend an older version failed to read the letter
    db.execute("UPDATE files SET catalogue_error='metadata: BadZipFile: old reader' "
               "WHERE logical_path='letters/letter.docx'")
    db.commit()
    before = rows(cfg)["file-cabinet:letters/letter.docx"]

    res = run_scan(cfg, src)  # a normal scan leaves it alone (unchanged)
    assert res["to_read"] == 0 and rows(cfg)["file-cabinet:letters/letter.docx"]["catalogue_error"]

    res = run_scan(cfg, src, reread="problems")
    assert res["status"] == "finished" and res["to_read"] == 1 and res["reread_files"] == 1
    after = rows(cfg)["file-cabinet:letters/letter.docx"]
    assert after["catalogue_error"] is None and after["triage_id"] == before["triage_id"]
    assert after["meta_title"] == "Letter to the Council"
    detail = db.execute("SELECT detail FROM actions WHERE action='re-read' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert detail == scan_mod.REREAD_DETAIL["problem"]

    assert run_scan(cfg, src, reread="problems")["to_read"] == 0  # nothing left with a problem
    n = len([k for k in rows(cfg) if k.startswith("file-cabinet:") and "::" not in k])
    res = run_scan(cfg, src, reread="all")
    assert res["to_read"] == res["reread_files"] == n == res["scanned"]
    db.close()

    with pytest.raises(ValueError):
        run_scan(cfg, src, reread="bogus")
    p = build_parser()
    assert p.parse_args(["triage", "scan", "--reread-problems"]).reread == "problems"
    assert p.parse_args(["triage", "scan", "--reread-all"]).reread == "all"
    assert p.parse_args(["triage", "scan"]).reread is None
    with pytest.raises(SystemExit):
        p.parse_args(["triage", "scan", "--reread-all", "--reread-problems"])


def test_web_scan_reread_argv():
    from imagepipe.web.server import build_argv

    assert build_argv("triage-scan", {}) == ["triage", "scan"]
    assert build_argv("triage-scan", {"source": "photos", "reread": "problems"}) == [
        "triage", "scan", "--source", "photos", "--reread-problems"]
    assert build_argv("triage-scan", {"reread": "all"}) == ["triage", "scan", "--reread-all"]
    assert build_argv("triage-scan", {"reread": "junk"}) == ["triage", "scan"]


# ------------------------------------------------- #16 routing decisions
def _row(**kw):
    base = dict(kind="document", ext="", mime="", catalogue_error=None, ext_mismatch=0, encrypted=0,
                takeout_title=None)
    return base | kw


def test_routing_decisions():
    from imagepipe.triage.kinds import classify

    zipped = dict(kind="archive", mime="application/zip")
    for ext in ("zip", "7z", "rar", "tar", "gz", "cab"):
        assert classify(_row(**zipped, ext=ext), {})[0] == "extract", ext
    for ext in ("xlsm", "dotx", "potx", "xltx", "xlsb", "vsdx", "vstx", "vssx", "odg", "oxps", "xps", "idml",
                "onepkg", "zed", "uzed", "ftmb", "doc", "xls", "webarchive", ""):
        assert classify(_row(**zipped, ext=ext), {}) == ("quarantine/unknown-type", "unrecognised-extension"), ext
    assert classify(_row(**zipped, ext="kmz"), {})[0] == "sorted/other-known"  # known zip-based types unchanged
    ole = dict(mime="application/x-ole-storage")
    for ext in ("ftw", "gno", "fbk", "mpp", "vsd", "pub", "tcw", "hmk", "cl3", "obd"):
        assert classify(_row(**ole, ext=ext), {})[0] == "sorted/other-known", ext
    for ext in ("tmp", "crdownload", "asd", "wbk"):
        assert classify(_row(**ole, ext=ext), {}) == ("quarantine/system-junk", f"temp-or-backup-{ext}"), ext
    assert classify(_row(**zipped, ext="crdownload"), {})[0] == "quarantine/system-junk"
    for ext in ("tibuq6", "no1doc", "xls whakapapa", ""):
        assert classify(_row(**ole, ext=ext), {}) == ("quarantine/unknown-type", "unrecognised-extension"), ext
    assert classify(_row(**ole, ext="doc"), {}) == ("docupipe", "document-doc")


def test_odd_extension_not_used_in_new_name():
    from imagepipe.triage.naming import build_name

    assert build_name("IPX", "2001", "Tree", "xls whakapapa") == "IPX_2001_Tree-xls-whakapapa"
    assert build_name("IPX", "2001", "Tree", "doc") == "IPX_2001_Tree.doc"
