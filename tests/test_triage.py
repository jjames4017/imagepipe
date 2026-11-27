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
  sources:
    - name: photos
      path: {vault}/photos
    - name: file-cabinet
      path: "{vault}/file cabinet"
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

    raw = {"database": "/t/db", "log_dir": "/t/l", "reports_dir": "/t/r",
           "sources": [{"name": "s", "path": "/s"}]}
    assert parse_triage(raw).embed_metadata == []


def test_config_rejects_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGEPIPE_FILESTACK", str(tmp_path / "src" / "filestack"))
    p = tmp_path / "c.yml"
    p.write_text(f"""
manifest: {tmp_path}/m.db
log_dir: {tmp_path}/l
reports_dir: {tmp_path}/r
triage:
  database: {tmp_path}/t.db
  log_dir: {tmp_path}/tl
  reports_dir: {tmp_path}/tr
  sources: [{{name: s, path: {tmp_path}/src}}]
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
    assert not (vault / "filestack" / "sorted" / "images").exists()

    r = rows(cfg)
    bali = r["photos:2014/Bali/IMG_1234.jpg"]
    assert bali["bucket"] == "images" and bali["date_source"] == "exif"
    dest = Path(bali["dest_path"])
    assert dest.parent == vault / "filestack" / "sorted" / "images" / "photos" / "2014" / "Bali"
    assert re.fullmatch(rf"{ID_PATTERN}_2014-07-12_.+\.jpg", dest.name), dest.name
    assert dest.name.startswith(bali["triage_id"])
    if HAS_GEO:
        assert bali["smartname_source"] == "gps+folder" and "-ID-" in bali["smartname"]
    xmp = r["photos:2014/Bali/IMG_1234.xmp"]
    assert xmp["companion_of"] == bali["id"] and xmp["dest_path"] == str(dest.with_suffix(".xmp"))

    dup = r["photos:old phone backup/DCIM/IMG_1234.jpg"]
    assert dup["bucket"] == "images" and dup["sha256"] == bali["sha256"]
    assert dup["dest_path"].startswith(str(vault / "filestack" / "sorted" / "images" / "photos" / "old phone backup" / "DCIM"))

    live = r["photos:2019/Mum 70th/IMG_5555.MOV"]
    still = r["photos:2019/Mum 70th/IMG_5555.jpg"]
    assert live["rule"] == "live-photo-clip" and live["bucket"] == "images"
    assert Path(live["dest_path"]).stem == Path(still["dest_path"]).stem
    assert "Mum-70th" in still["smartname"]

    pxl = r["photos:Takeout/Google Photos/Trip to Hoi An/PXL_20221003_101010123.jpg"]
    tj = r["photos:Takeout/Google Photos/Trip to Hoi An/PXL_20221003_101010123.jpg.supplemental-metad.json"]
    assert tj["companion_of"] == pxl["id"] and tj["dest_path"] == pxl["dest_path"] + ".json"
    assert pxl["date_source"] == "sidecar-takeout" and pxl["name_date"] == "2022-10-03_101010"
    if HAS_GEO:
        assert pxl["smartname"].startswith("Hoi-An-VN")

    assert r["photos:junk/.DS_Store"]["bucket"] == "quarantine/system-junk"
    assert r["photos:junk/empty.jpg"]["bucket"] == "quarantine/zero-byte"
    assert r["photos:junk/not_really.jpg"]["bucket"] == "quarantine/corrupt"
    assert r["photos:misc/mystery.qqq"]["bucket"] == "quarantine/unknown-type"
    assert r["photos:misc/clip.mp4"]["bucket"] == "video"
    assert r["photos:misc/song.mp3"]["bucket"] == "audio"
    assert r["photos:misc/setup.exe"]["bucket"] == "software"
    assert r["photos:misc/app.ts"]["bucket"] == "code"
    assert r["photos:misc/clip.mp4"]["dest_path"].startswith(str(vault / "filestack" / "sorted" / "video" / "photos" / "misc"))

    letter = r["file-cabinet:letters/letter.docx"]
    assert letter["bucket"] == "documents" and letter["name_date"] == "2016-03-04_050607"
    assert letter["smartname"] == "letters-letter"  # a descriptive file name beats the Office title (#23)
    assert Path(letter["dest_path"]).parent == vault / "filestack" / "sorted" / "documents" / "file cabinet" / "letters"
    receipt = r["file-cabinet:Tax/2019/receipt.pdf"]
    assert receipt["bucket"] == "documents" and receipt["smartname"] == "Tax-receipt"
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
    assert photo["bucket"] == "images" and photo["origin"].endswith("Fiji 2010.zip::day1/photo1.jpg")
    assert Path(photo["current_path"]).parent == vault / "filestack" / "sorted" / "images" / "file cabinet" / "Holidays" / "Fiji 2010" / "day1"
    assert r["file-cabinet:Holidays/Fiji 2010/itinerary.pdf"]["bucket"] == "documents"
    inner = r["file-cabinet:Holidays/Fiji 2010/inner/a.txt"]
    assert inner["status"] == "done" and inner["bucket"] == "documents"
    assert r["file-cabinet:Holidays/backup/old/notes2.txt"]["status"] == "done"

    locked = r["file-cabinet:Holidays/locked.zip"]
    assert locked["bucket"] == "quarantine/encrypted" and Path(locked["current_path"]).is_file()
    assert Path(locked["current_path"]).parent == vault / "filestack" / "sorted" / "quarantine" / "encrypted" / "file cabinet" / "Holidays"
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
    row = db.execute("SELECT * FROM files WHERE status='planned' AND bucket='documents' LIMIT 1").fetchone()
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
    dest_dir = os.path.join(os.fsencode(vault / "filestack" / "sorted" / "images" / "photos"), b"caf\xe9 trip")
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
    assert r["file-cabinet:art/war.PSD"]["bucket"] == "specials"  # design files kept aside (#55)
    assert r["file-cabinet:fin/Banking Details.docx"]["bucket"] == "quarantine/encrypted"
    assert r["file-cabinet:fin/old format.xlsx"]["bucket"] == "documents"


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
    assert classify(_row(**zipped, ext="kmz"), {})[0] == "other-known"  # known zip-based types unchanged
    ole = dict(mime="application/x-ole-storage")
    for ext in ("ftw", "gno", "fbk", "mpp", "vsd", "pub", "tcw", "hmk", "cl3", "obd"):
        assert classify(_row(**ole, ext=ext), {})[0] == "other-known", ext
    for ext in ("tmp", "crdownload", "asd", "wbk"):
        assert classify(_row(**ole, ext=ext), {}) == ("quarantine/system-junk", f"temp-or-backup-{ext}"), ext
    assert classify(_row(**zipped, ext="crdownload"), {})[0] == "quarantine/system-junk"
    for ext in ("tibuq6", "no1doc", "xls whakapapa", ""):
        assert classify(_row(**ole, ext=ext), {}) == ("quarantine/unknown-type", "unrecognised-extension"), ext
    assert classify(_row(**ole, ext="doc"), {}) == ("documents", "document-doc")


def test_odd_extension_not_used_in_new_name():
    from imagepipe.triage.naming import build_name

    assert build_name("IPX", "2001", "Tree", "xls whakapapa") == "IPX_2001_Tree-xls-whakapapa"
    assert build_name("IPX", "2001", "Tree", "doc") == "IPX_2001_Tree.doc"


# ------------------------------------------------- #18 smartname characters
def test_smartname_characters_and_cut():
    from imagepipe.triage.naming import clean_part, cut_at_hyphen, smartname

    assert clean_part("logo_top (3)") == "logo-top-3"
    assert clean_part("Letters & Envelopes") == "Letters-and-Envelopes"
    assert clean_part("Ann Dansey, Bill & Reima Hall 2") == "Ann-Dansey-Bill-and-Reima-Hall-2"
    assert clean_part("picnan's94thbirthday") == "picnans94thbirthday"
    assert clean_part("Andrew Kusabs (Andrew@kusabs.co.nz)") == "Andrew-Kusabs-Andrew-kusabs-co-nz"
    assert clean_part("Māori whakapapa – Ōtaki [draft] #2!") == "Māori-whakapapa-Ōtaki-draft-2"
    assert clean_part("bad\udc80bytes") == "bad-bytes"
    assert cut_at_hyphen("Andrew-Kusabs-Andrew-kusabs", 18) == "Andrew-Kusabs"
    assert cut_at_hyphen("Andrew-Kusabs-Andrew", 13) == "Andrew-Kusabs"
    assert cut_at_hyphen("Supercalifragilistic", 5) == "Super"
    name, _ = smartname(None, None, ["Letters & Envelopes"], "Colleens Address (big) envelope", 30)
    assert name == "Letters-and-Envelopes-Colleens"


# ------------------------------------------------- #19 time in names
def test_time_in_name_date_and_not_in_smartname():
    from imagepipe.triage.naming import date_from_path, descriptive_filename, name_date_of, smartname

    assert name_date_of("2016-05-15 06:43:31") == "2016-05-15_064331"
    assert name_date_of("2016-05-15 00:00:00") == "2016-05-15"
    assert name_date_of("2016-05-15") == "2016-05-15"
    assert date_from_path("IMG_20190429_164338.jpg", []) == ("2019-04-29_164338", "filename")
    assert date_from_path("WP_20160515_06_43_31_Pro.jpg", []) == ("2016-05-15_064331", "filename")
    assert date_from_path("PXL_20221003_101010123.jpg", []) == ("2022-10-03_101010", "filename")
    assert date_from_path("VID_20200101_2359.mp4", []) == ("2020-01-01_2359", "filename")
    assert date_from_path("scan 20190429 1234567.pdf", []) == ("2019-04-29", "filename")  # a counter, not a time
    assert date_from_path("x.jpg", ["2019", "Bali 2019-07-03 101010"]) == ("2019-07-03", "folder")  # folders: no time
    assert descriptive_filename("WP_20160515_06_43_31_Pro") is None  # camera name, like IMG_
    assert descriptive_filename("Board report 20191108_0930 final") == "Board report final"
    name, _ = smartname(None, None, ["Docs", "Review_Griffith"], "WP_20160515_06_43_31_Pro")
    assert "06" not in name and "Pro" not in name


# ------------------------------------------------- #17 saved web pages
def test_saved_web_pages_stay_together(env):
    cfg, vault = env
    html = b"<!DOCTYPE html><html><head><title>Gwlad</title></head><body><img src='Gwlad_files/logo.gif'></body></html>"
    build(vault / "file cabinet", {
        "Genealogy/Gwlad.htm": html,
        "Genealogy/Gwlad_files/logo_top (3).gif": jpeg(),
        "Genealogy/Gwlad_files/style.css": b"body { color: red; }\n",
        "Genealogy/Gwlad_files/sub/deep.jpg": jpeg(),
        "Genealogy/Lonely.html": html,
        "Saved/Old page_files/a.css": b"p { margin: 0; }\n",
        "Saved/Old page_files/pic.jpg": jpeg(),
        "Mine/my_files/holiday.jpg": jpeg(),
        "Saved/Whole page.mht": b"From: <Saved by Windows Internet Explorer>\r\nMIME-Version: 1.0\r\n\r\nhello\r\n",
    })
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    root = vault / "filestack" / "sorted" / "web-pages" / "file cabinet"
    page = r["file-cabinet:Genealogy/Gwlad.htm"]
    assert page["bucket"] == "web-pages" and page["rule"] == "web-page"
    assert Path(page["dest_path"]).parent == root / "Genealogy"
    assert Path(page["dest_path"]).name.startswith(page["triage_id"] + "_")  # the page gets an ID name
    for rel in ("Gwlad_files/logo_top (3).gif", "Gwlad_files/style.css", "Gwlad_files/sub/deep.jpg"):
        x = r[f"file-cabinet:Genealogy/{rel}"]
        assert x["bucket"] == "web-pages" and x["rule"] == "web-page-files", rel
        assert x["dest_path"] == str(root / "Genealogy" / rel), rel  # folder and names kept
        assert x["triage_id"]
    assert r["file-cabinet:Genealogy/Lonely.html"]["bucket"] == "documents"  # no _files folder: a normal document
    assert r["file-cabinet:Saved/Old page_files/pic.jpg"]["dest_path"] == str(root / "Saved" / "Old page_files" / "pic.jpg")
    assert r["file-cabinet:Mine/my_files/holiday.jpg"]["bucket"] == "images"  # not a web folder
    assert r["file-cabinet:Saved/Whole page.mht"]["bucket"] == "web-pages"


# ------------------------------------------------- #14 plan speed
def test_plan_folder_query_uses_folder_index(env):
    cfg, vault = env
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    db = sqlite3.connect(cfg.triage.database)
    plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM files WHERE status IN ('catalogued','planned') "
                      "AND source=? AND parent_id=? AND dir=? ORDER BY rel_path", ("file-cabinet", 0, "x")).fetchall()
    text = " ".join(str(r[-1]) for r in plan)
    assert "idx_tfiles_dir_path" in text and "TEMP B-TREE" not in text, text
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_tfiles_dir" not in names and "idx_tfiles_tid" in names
    db.close()


def test_plan_folder_error_only_undoes_that_folder(env, monkeypatch):
    from imagepipe.triage import plan as plan_mod

    cfg, vault = env
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    real = plan_mod.Planner.plan_group

    def flaky(self, rows, source_folder, progress):
        if rows and rows[0]["dir"] == "letters":
            for r in rows:  # half-done work in the failing folder must be undone
                self.t.update(r["id"], bucket="documents", status="planned")
            raise RuntimeError("boom")
        return real(self, rows, source_folder, progress)

    monkeypatch.setattr(plan_mod.Planner, "plan_group", flaky)
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    letter = r["file-cabinet:letters/letter.docx"]
    assert letter["status"] == "error" and letter["bucket"] is None
    assert r["file-cabinet:Tax/2019/receipt.pdf"]["status"] == "planned"


# ------------------------------------------------- #4 damaged EXIF
def bad_exif_jpeg() -> bytes:
    import struct

    # an IFD that says 3 entries but holds 1: Pillow warns "Corrupt EXIF data"
    tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 3) + struct.pack("<HHI4s", 0x0112, 3, 1, b"\x01\0\0\0")
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG", exif=b"Exif\x00\x00" + tiff)
    return buf.getvalue()


def test_damaged_exif_is_counted_not_printed(env, capfd):
    from imagepipe.triage.meta import image_meta

    cfg, vault = env
    p = vault / "file cabinet" / "pics" / "bad.jpg"
    build(vault / "file cabinet", {"pics/bad.jpg": bad_exif_jpeg(), "pics/good.jpg": jpeg()})
    assert image_meta(str(p)).get("exif_damaged") == 1
    assert "exif_damaged" not in image_meta(str(vault / "file cabinet" / "pics" / "good.jpg"))
    capfd.readouterr()
    res = run_scan(cfg, cfg.triage.source("file-cabinet"))
    err = capfd.readouterr().err
    assert res["status"] == "finished" and res["exif_damaged"] == 1 and res["read_problems"] == 0
    assert "Corrupt EXIF" not in err
    r = rows(cfg)["file-cabinet:pics/bad.jpg"]
    assert r["catalogue_error"] is None and r["kind"] == "image"


# ------------------------------------------------- #15 geocoder message
def test_geocoder_message_is_imagepipes_own(capfd):
    from imagepipe.triage.naming import Geocoder

    g = Geocoder(True)
    if not g.available:
        pytest.skip("reverse_geocoder not installed")
    g.places([(-8.65, 115.13)])
    g.places([(15.88, 108.33)])
    out = capfd.readouterr()
    assert "Loading formatted geocoded file" not in out.out + out.err
    assert out.err.count("Loading place names (offline)... done") == 1


# ------------------------------------------------- 0.3.6: #23-#26
def test_14_digit_timestamps_and_apostrophes():
    from imagepipe.triage.naming import clean_part, date_from_path, descriptive_filename

    assert date_from_path("DVR_Mill Street_20121124004613_20121124004642_1607059_0001.mp4", []) == (
        "2012-11-24_004613", "filename")
    assert date_from_path("cam_20121124256199.mp4", []) is None  # hour 25: not a timestamp
    assert "004613" not in (descriptive_filename("DVR_Mill Street_20121124004613_x") or "")
    assert clean_part("If you’re looking") == "If-youre-looking"
    assert clean_part("Tom‘s ʼbestʼ") == "Toms-best"


def test_default_clock_dates():
    from imagepipe.triage.naming import is_default_clock

    for d in ("2006-01-01 12:00:22", "2000-01-01 00:00:05", "2004-01-01 01:59:00", "1980-01-01", "2010-01-01 12:30:00"):
        assert is_default_clock(d), d
    for d in ("2006-01-02 12:00:22", "2014-01-01 12:00:00", "2006-01-01 18:30:00", "2006-02-01 00:00:00", None, ""):
        assert not is_default_clock(d), d


def test_plan_date_choices(env):
    cfg, vault = env
    build(vault / "file cabinet", {
        "Reviewed/2010-01-09 Reviewed Keep/100_0907.jpg": jpeg(date="2006:01:01 12:00:22"),
        "Unsorted/100_0908.jpg": jpeg(date="2006:01:01 12:00:22"),
        "Unsorted/New Year.jpg": jpeg(date="2019:01:01 00:05:00"),
        "Work/BCP DVCR_MASTER 17 04 2020_FINAL.docx": docx("Risk Assessment Register", "2006-05-12T00:59:57Z"),
        "Work/Q.docx": docx("Quarterly Board Report", "2018-03-04T05:06:07Z"),
    })
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    reviewed = r["file-cabinet:Reviewed/2010-01-09 Reviewed Keep/100_0907.jpg"]
    assert (reviewed["name_date"], reviewed["date_source"]) == ("2010-01-09", "folder")  # camera clock not set
    lone = r["file-cabinet:Unsorted/100_0908.jpg"]
    assert (lone["name_date"], lone["date_source"]) == ("2006-01-01_120022", "exif-default-clock")  # nothing better
    ny = r["file-cabinet:Unsorted/New Year.jpg"]
    assert (ny["name_date"], ny["date_source"]) == ("2019-01-01_000500", "exif")  # not a default year
    bcp = r["file-cabinet:Work/BCP DVCR_MASTER 17 04 2020_FINAL.docx"]
    assert (bcp["name_date"], bcp["date_source"]) == ("2020-04-17", "filename")
    assert "Risk" not in bcp["smartname"] and "BCP-DVCR-MASTER" in bcp["smartname"]
    q = r["file-cabinet:Work/Q.docx"]  # file name not descriptive: the Office title and date are used
    assert q["smartname"].endswith("Quarterly-Board-Report") and q["date_source"] == "office"


# ------------------------------------------------- 0.3.7: #28, #29
def test_joining_words_trimmed_after_dates():
    from imagepipe.triage.naming import meaningful_folder

    assert meaningful_folder("2001 & 2002 papers") == "papers"
    assert meaningful_folder("2010 - Holiday") == "Holiday"
    assert meaningful_folder("Jan to March 2005") is None
    assert meaningful_folder("Photos 2004 and 2005 Fiji") == "Photos and Fiji"  # middle words stay
    assert meaningful_folder("Bill & Reima 2004") == "Bill & Reima"


def test_ddmmyy_only_with_a_matching_folder_year():
    from imagepipe.triage.naming import date_from_path, ddmmyy_in

    folders = ["Victoria Studies", "2001 & 2002 papers", "Psyc 121"]
    assert date_from_path("103 Let Shakespeare 120301.mht", folders) == ("2001-03-12", "filename")
    assert ddmmyy_in("103 Let Shakespeare 120301", folders) == ("2001-03-12", "120301")
    assert date_from_path("Track 120301.mp3", ["Music"]) is None  # no folder year: a counter
    assert date_from_path("Track 120301.mp3", ["Music 2015"]) == ("2015", "folder")  # year too far off
    assert date_from_path("x 310299.doc", ["1999"]) == ("1999", "folder")  # 31/02 is not a date
    assert date_from_path("Lecture 050401.htm", ["2001"]) == ("2001-04-05", "filename")
    assert date_from_path("Scan 2001-04-05 120301.pdf", ["2001"]) == ("2001-04-05_120301", "filename")  # full date wins; digits after it are a time


def test_plan_uses_ddmmyy_and_drops_it_from_the_name(env):
    cfg, vault = env
    build(vault / "file cabinet", {"Uni/2001 & 2002 papers/Psyc 121/103 Let Shakespeare 120301.txt": b"notes\n"})
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)["file-cabinet:Uni/2001 & 2002 papers/Psyc 121/103 Let Shakespeare 120301.txt"]
    assert (r["name_date"], r["date_source"]) == ("2001-03-12", "filename")
    assert "120301" not in r["smartname"] and not r["smartname"].startswith("and")
    assert r["smartname"] == "2002-papers-Psyc-121-103-Let-Shakespeare"  # 2002 isn't the date used: kept (#47)


# ------------------------------------------------- 0.3.8: #31-#33
def test_office_lock_files_and_generic_titles():
    from imagepipe.triage.kinds import classify
    from imagepipe.triage.meta import is_generic_title

    lock = _row(mime="application/vnd.openxmlformats", ext="pptx", rel_path="Meetings/~$Regional Councils 2.pptx")
    assert classify(lock, {}) == ("quarantine/system-junk", "office-lock-file")
    real = _row(mime="application/vnd.openxmlformats", ext="pptx", rel_path="Meetings/Regional Councils 2.pptx")
    assert classify(real, {})[0] == "documents"
    for t in ("Track 23", "track23", "Audio Track 4", "Chapter 7", "Untitled", "title"):
        assert is_generic_title(t), t
    for t in ("Paddlin Madelin Home", "Track and field results", "Chapter of my life"):
        assert not is_generic_title(t), t


def test_plan_url_encoded_names_and_office_ddmmyy(env):
    import sqlite3 as _sq

    cfg, vault = env
    build(vault / "file cabinet", {
        "My Music/Cliff%20Edwards%20-%20Glendale%20-%2023%20-%20Paddlin'%20Madelin'%20Home[1].txt": b"x\n",
        "Weekly meetings/Minutes_ Project Kickoff meeting_310516(ui6Y).docx":
            docx("Agenda Template", "2015-11-04T04:55:00Z"),
        "Weekly meetings/Minutes 310599.docx": docx("Agenda Template", "2015-11-04T04:55:00Z"),
    })
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    db = _sq.connect(cfg.triage.database)  # as if an audio tag had said "Track 23"
    db.execute("UPDATE files SET meta_title='Track 23' WHERE rel_path LIKE 'My Music/Cliff%'")
    db.commit()
    db.close()
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    song = next(v for k, v in r.items() if "Cliff" in k)
    assert "2023" not in song["name_date"] and song["date_source"] != "filename"
    assert song["smartname"].endswith("Cliff-Edwards-Glendale-23-Paddlin-Madelin-Home-1"), song["smartname"]
    mins = r["file-cabinet:Weekly meetings/Minutes_ Project Kickoff meeting_310516(ui6Y).docx"]
    assert (mins["name_date"], mins["date_source"]) == ("2016-05-31", "filename")
    assert "310516" not in mins["smartname"]
    far = r["file-cabinet:Weekly meetings/Minutes 310599.docx"]  # 1999 is nowhere near the 2015 Office date
    assert far["date_source"] == "office" and far["name_date"].startswith("2015-11-04")


# ------------------------------------------------- 0.3.9: #35-#39
def cam_jpeg(model: str, date: str) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "EASTMAN KODAK COMPANY"
    exif[0x0110] = model
    exif.get_ifd(0x8769)[0x9003] = date
    buf = io.BytesIO()
    Image.new("RGB", (40, 30)).save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_names_hex_ids_joined_months_quarantine_original():
    from imagepipe.triage.naming import date_in_text, smartname, with_original_name

    assert date_in_text("11WTBDMay2009TrsLst") == "2009-05"
    assert date_in_text("ReportSeptember2015") == "2015-09"
    assert date_in_text("Germany2009") == "2009"
    name, _ = smartname(None, None, ["Cards"], "!cid_3374B1F2973C4F57B07B4D160F62B985@EvaAnnPC")
    assert name == "Cards-cid-EvaAnnPC"
    name, _ = smartname(None, None, ["x"], "scan 123e4567-e89b-12d3-a456-426614174000 receipt")
    assert name == "receipt"  # UUID gone (and the leading "scan", as before)
    assert with_original_name("Harddrive-WTB-Dansey", "B8883000") == "Harddrive-WTB-Dansey-B8883000"
    long = with_original_name("A-very-long-folder-name-" * 5, "Original File (2)", 40)
    assert long.endswith("Original-File-2") and len(long) <= 40
    assert with_original_name("Docs-B8883000", "B8883000") == "Docs-B8883000"


def test_plan_drifting_clock_ebook_years_quarantine_names(env):
    cfg, vault = env
    build(vault / "file cabinet", {
        "Pics/Old/100_0907.jpg": cam_jpeg("EASYSHARE C330", "2006:01:01 12:00:22"),
        "Pics/Camera Pics 19-03-2014/100_2422.jpg": cam_jpeg("EASYSHARE C330", "2006:01:25 15:44:58"),
        "Pics/Unsorted/100_2500.jpg": cam_jpeg("EASYSHARE C330", "2006:02:01 10:00:00"),
        "Pics/Other cam 19-03-2014/IMG_1.jpg": cam_jpeg("PowerShot", "2006:01:25 15:44:58"),
        "ebooks/reima/Red Inferno_ 1945 - Robert Conroy.epub": b"PK\x03\x04" + b"\x00" * 40,
        "WTB 2008-2009/B8883000": b"\x8f\x00\x13\x37" * 64,
    })
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    drift = r["file-cabinet:Pics/Camera Pics 19-03-2014/100_2422.jpg"]
    assert (drift["name_date"], drift["date_source"]) == ("2014-03-19", "folder")
    alone = r["file-cabinet:Pics/Unsorted/100_2500.jpg"]  # nothing better: kept, but marked
    assert (alone["name_date"], alone["date_source"]) == ("2006-02-01_100000", "exif-default-clock")
    other = r["file-cabinet:Pics/Other cam 19-03-2014/IMG_1.jpg"]  # a different camera is trusted
    assert (other["name_date"], other["date_source"]) == ("2006-01-25_154458", "exif")
    book = r["file-cabinet:ebooks/reima/Red Inferno_ 1945 - Robert Conroy.epub"]
    assert book["name_date"] != "1945" and "1945" in book["smartname"]
    q = r["file-cabinet:WTB 2008-2009/B8883000"]
    assert q["bucket"] == "quarantine/unknown-type" and q["smartname"].endswith("B8883000")


# ------------------------------------------------- 0.3.10: #41-#43
def test_drift_only_replaced_by_a_date_far_away(env):
    cfg, vault = env
    build(vault / "file cabinet", {
        "NYE/2008 New Year/party.jpg": cam_jpeg("DSC-W55", "2008:01:01 00:15:00"),  # a real New Year photo
        "Trips/Fiji 2008/beach.jpg": cam_jpeg("DSC-W55", "2008:07:14 10:20:30"),
        "Pics/Camera Pics 19-03-2014/100_2422.jpg": cam_jpeg("DSC-W55", "2008:02:25 15:44:58"),
    })
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    fiji = r["file-cabinet:Trips/Fiji 2008/beach.jpg"]  # folder year agrees: the EXIF date is trusted
    assert (fiji["name_date"], fiji["date_source"]) == ("2008-07-14_102030", "exif")
    far = r["file-cabinet:Pics/Camera Pics 19-03-2014/100_2422.jpg"]  # folder years away: EXIF replaced
    assert (far["name_date"], far["date_source"]) == ("2014-03-19", "folder")


def test_genealogy_and_thumbnail_cache_routing():
    from imagepipe.triage.kinds import classify

    for ext in ("paf", "ged"):
        assert classify(_row(kind="unknown", ext=ext, rel_path=f"x/tree.{ext}"), {})[0] == "other-known"
        assert classify(_row(mime="text/plain", ext=ext, rel_path=f"x/tree.{ext}"), {})[0] == "other-known"
    db = _row(kind="unknown", ext="db", rel_path="Pics/Funeral/photothumb.db")
    assert classify(db, {}) == ("quarantine/system-junk", "system-junk-file")
    assert classify(_row(kind="unknown", ext="db", rel_path="x/family.db"), {})[0] == "other-known"


def test_repeated_words_dropped():
    from imagepipe.triage.naming import drop_repeats, smartname

    assert drop_repeats("Photos-Dallas-Tito-Dallas-2-Dallas-2-003") == "Photos-Dallas-Tito-Dallas-2-003"
    assert drop_repeats("Dansey-Dansey-Census") == "Dansey-Census"
    assert drop_repeats("dansey-Dansey") == "dansey"
    assert drop_repeats("Flat-1-1") == "Flat-1-1"
    name, _ = smartname(None, None, ["Dansey", "Dansey"], "Some Dansey Birth dates")
    assert name == "Dansey-Some-Dansey-Birth-dates"


# ------------------------------------------------- 0.3.11: #45
EML = (b"From: Nanna <nanna@example.nz>\r\nTo: Tu <tu@example.nz>\r\n"
       b"Date: Mon, 12 Mar 2001 21:05:09 +1300\r\nSubject: Nanna, Father & Mum\r\n\r\nHello\r\n")


def test_eml_date_header(tmp_path):
    from imagepipe.triage.meta import eml_meta

    p = tmp_path / "x.eml"
    p.write_bytes(EML)
    assert eml_meta(str(p), "Australia/Brisbane") == {"meta_date": "2001-03-12 18:05:09", "meta_date_src": "email"}
    assert eml_meta(str(p)) == {"meta_date": "2001-03-12 21:05:09", "meta_date_src": "email"}
    p.write_bytes(b"From: a@b\r\nDate: not a date\r\n\r\nx")
    assert eml_meta(str(p), "Australia/Brisbane") == {}
    p.write_bytes(b"From: a@b\r\n\r\nDate: Mon, 12 Mar 2001 21:05:09 +1300\r\n")  # in the body: not a header
    assert eml_meta(str(p), "Australia/Brisbane") == {}


def test_reread_ext_and_email_dates(env):
    from imagepipe.cli import build_parser
    from imagepipe.web.server import build_argv

    cfg, vault = env
    src = cfg.triage.source("file-cabinet")
    build(vault / "file cabinet", {"Old Documents/Nanna, Father & Mum.eml": EML})
    assert run_scan(cfg, src)["status"] == "finished"
    db = sqlite3.connect(cfg.triage.database)  # as if read by a version without the Date: reader
    db.execute("UPDATE files SET meta_date=NULL, meta_date_src=NULL WHERE ext='eml'")
    db.commit()
    db.close()
    assert run_scan(cfg, src)["to_read"] == 0
    res = run_scan(cfg, src, reread="ext", reread_ext=["EML"])
    assert res["to_read"] == res["reread_files"] == 1
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)["file-cabinet:Old Documents/Nanna, Father & Mum.eml"]
    assert r["bucket"] == "email" and (r["name_date"], r["date_source"]) == ("2001-03-12_180509", "email")

    with pytest.raises(ValueError):
        run_scan(cfg, src, reread="ext", reread_ext=[])
    p = build_parser()
    a = p.parse_args(["triage", "scan", "--reread-ext", "eml,msg"])
    assert a.reread_ext == "eml,msg" and a.reread is None
    with pytest.raises(SystemExit):
        p.parse_args(["triage", "scan", "--reread-ext", "eml", "--reread-all"])
    assert build_argv("triage-scan", {"reread": "ext", "reread_ext": " .EML, msg "}) == [
        "triage", "scan", "--reread-ext", "eml,msg"]
    with pytest.raises(ValueError):
        build_argv("triage-scan", {"reread": "ext", "reread_ext": ""})


# ------------------------------------------------- 0.3.12: #47
def test_dates_kept_in_name_when_not_the_one_used():
    from imagepipe.triage.naming import meaningful_folder, smartname

    assert smartname(None, None, ["Friends"], "Thelma Reynolds 1984", used_date="2015-09-16_164939")[0] == \
        "Friends-Thelma-Reynolds-1984"
    assert smartname(None, None, ["Friends"], "Thelma Reynolds 1984", used_date="1984")[0] == "Friends-Thelma-Reynolds"
    assert meaningful_folder("Sandgate 2005", "2005-08-27_110734") == "Sandgate"
    assert meaningful_folder("Camera Pics 19-03-2014", "2014-03-19") == "Camera Pics"
    assert meaningful_folder("Camera Pics 19-03-2014", "2006-01-25_154458") == "Camera Pics 19-03-2014"
    assert meaningful_folder("July 2014 Trip", "2014-07-03") == "Trip"
    assert meaningful_folder("July 2014 Trip", "2015-01-01") == "July 2014 Trip"
    assert meaningful_folder("Dallas Dec 2013.2", "2013-12-23_101749") == "Dallas .2"
    assert meaningful_folder("2001 & 2002 papers") == "papers"  # no date given: all dates go, as before


def test_plan_keeps_old_year_on_a_scanned_photo(env):
    cfg, vault = env
    build(vault / "file cabinet", {"Friends/Thelma Reynolds 1984.jpg": jpeg(date="2015:09:16 16:49:39")})
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)["file-cabinet:Friends/Thelma Reynolds 1984.jpg"]
    assert r["name_date"] == "2015-09-16_164939" and r["smartname"].endswith("Thelma-Reynolds-1984")


# ------------------------------------------------- 0.3.15: #55 specials
def test_specials_routing():
    from imagepipe.triage.kinds import classify

    for ext in ("psd", "psb", "svg", "eps", "ai", "cdr", "wmf", "emf", "ps", "dxf", "dwg", "xcf", "kra",
                "afdesign", "afphoto", "indd", "mpo", "ico"):
        for kind in ("image", "unknown", "document"):
            assert classify(_row(kind=kind, ext=ext, rel_path=f"x/a.{ext}"), {}) == (
                "specials", f"special-{ext}"), (ext, kind)
    assert classify(_row(kind="empty", ext="psd", rel_path="x/a.psd"), {})[0] == "quarantine/zero-byte"
    for ext in ("heic", "gif", "webp", "png", "jpg"):
        assert classify(_row(kind="image", ext=ext, rel_path=f"x/a.{ext}"), {})[0] == "images", ext


def test_plan_sends_odd_tiffs_to_specials(env):
    np = pytest.importorskip("numpy")
    tifffile = pytest.importorskip("tifffile")
    cfg, vault = env
    folder = vault / "file cabinet" / "scans"
    folder.mkdir(parents=True)
    img = np.full((20, 30, 3), 90, dtype=np.uint8)
    with tifffile.TiffWriter(folder / "two.tif") as tw:
        tw.write(img, photometric="rgb")
        tw.write(img[:5, :5], photometric="rgb")
    tifffile.imwrite(folder / "layers.tif", img, photometric="rgb", extratags=[(37724, "B", 4, b"Adob", False)])
    Image.new("RGB", (30, 20)).save(folder / "plain.tif")
    assert run_scan(cfg, cfg.triage.source("file-cabinet"))["status"] == "finished"
    assert run_plan(cfg)["status"] == "finished"
    r = rows(cfg)
    assert (r["file-cabinet:scans/two.tif"]["bucket"], r["file-cabinet:scans/two.tif"]["rule"]) == (
        "specials", "special-tiff-pages")
    assert r["file-cabinet:scans/layers.tif"]["rule"] == "special-tiff-layered"
    assert r["file-cabinet:scans/plain.tif"]["bucket"] == "images"
    assert r["file-cabinet:scans/two.tif"]["dest_path"].startswith(str(vault / "filestack" / "sorted" / "specials" / "file cabinet"))
