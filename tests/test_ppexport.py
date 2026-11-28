"""PhotoPrism export: a fake PhotoPrism API, copies with the PhotoPrism work written in, phase 2 order."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from imagepipe.config import load_config
from imagepipe.manifest import Manifest
from imagepipe.ppexport import PPExportConfig, faces_for, plan_photo, run_export
from imagepipe.phase2.run import run_phase2
from tests.test_phase2 import jpeg

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def photo(uid, files, **kw):
    p = {"UID": uid, "TakenSrc": "meta", "TitleSrc": "", "CaptionSrc": "", "PlaceSrc": "", "Favorite": False,
         "Private": False, "DeletedAt": None, "Details": {}, "Albums": [], "Files": files}
    p.update(kw)
    return p


class FakePP:
    def __init__(self, photos, archived):
        self.photos = {p["UID"]: p for p in photos + archived}
        self.live = [p["UID"] for p in photos]
        self.archived = [p["UID"] for p in archived]
        self.logins = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n))
                if self.path == "/api/v1/session" and data == {"username": "admin", "password": "pw"}:
                    fake.logins += 1
                    return self.send({"access_token": "tok"})
                self.send({"error": "no"}, 401)

            def do_DELETE(self):
                self.send({})

            def do_GET(self):
                if self.headers.get("X-Auth-Token") != "tok":
                    return self.send({"error": "auth"}, 401)
                from urllib.parse import parse_qs, urlparse
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/api/v1/photos":
                    uids = fake.archived if q.get("archived") == ["true"] else fake.live
                    off, cnt = int(q["offset"][0]), int(q["count"][0])
                    return self.send([{"UID": x} for x in uids[off:off + cnt]])
                if u.path.startswith("/api/v1/photos/"):
                    return self.send(fake.photos[u.path.rsplit("/", 1)[1]])
                self.send({}, 404)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def sha1(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def setup(tmp_path, monkeypatch):
    stack = tmp_path / "filestack"
    monkeypatch.setenv("IMAGEPIPE_FILESTACK", str(stack))
    orig = tmp_path / "originals"
    b = orig / "batch_01" / "trip"
    b.mkdir(parents=True)
    (b / "a.jpg").write_bytes(jpeg())
    (b / "b.jpg").write_bytes(jpeg(color=(1, 2, 3), date="2019:05:06 07:08:09"))
    (b / "live.jpg").write_bytes(jpeg(color=(9, 9, 9)))
    (b / "live.mov").write_bytes(b"moov fake clip")
    (b / "a.jpg.yml").write_text("PhotoPrism sidecar")
    (b / "notes.txt").write_text("not a photo")
    (orig / "library").mkdir()
    (orig / "library" / "cache.bin").write_bytes(b"x")
    a, bb = b / "a.jpg", b / "b.jpg"
    photos = [
        photo("pa", [{"UID": "fa", "Name": "batch_01/trip/a.jpg", "Root": "/", "Hash": sha1(a), "MediaType": "image",
                      "Width": 1200, "Height": 900, "Markers": [
                          {"Type": "face", "Name": "Tonia", "X": 0.1, "Y": 0.2, "W": 0.2, "H": 0.3, "SubjSrc": "manual"},
                          {"Type": "face", "Name": "", "X": 0.5, "Y": 0.5, "W": 0.1, "H": 0.1},
                          {"Type": "face", "Name": "Bob", "X": 0.6, "Y": 0.1, "W": 0.1, "H": 0.1, "SubjSrc": "marker"},
                          {"Type": "face", "Name": "Gone", "X": 0.6, "Y": 0.1, "W": 0.1, "H": 0.1, "Invalid": True}]},
                     {"UID": "fy", "Name": "batch_01/trip/a.jpg.yml", "Root": "sidecar", "Hash": "x"}],
              Title="Beach day", TitleSrc="manual", Caption="AI words", CaptionSrc="ollama", Favorite=True,
              Details={"Keywords": "sand, sea", "KeywordsSrc": "manual"},
              Albums=[{"Type": "album", "Title": "Holidays"}, {"Type": "folder", "Title": "trip"},
                      {"Type": "month", "Title": "Jan 2020"}],
              TakenSrc="manual", TakenAtLocal="2020-01-02T10:00:00Z", TimeZone="Australia/Brisbane",
              PlaceSrc="estimate", Lat=-27.4, Lng=153.0),
        photo("pl", [{"UID": "f1", "Name": "batch_01/trip/live.jpg", "Root": "/", "MediaType": "image",
                      "Hash": sha1(b / "live.jpg")},
                     {"UID": "f2", "Name": "batch_01/trip/live.mov", "Root": "/", "MediaType": "live",
                      "Hash": sha1(b / "live.mov")}]),
    ]
    archived = [photo("pb", [{"UID": "fb", "Name": "batch_01/trip/b.jpg", "Root": "/", "Hash": sha1(bb),
                              "MediaType": "image"}], DeletedAt="2025-01-01T00:00:00Z",
                      Caption="My words", CaptionSrc="batch", PlaceSrc="manual", Lat=-27.5, Lng=153.1)]
    fake = FakePP(photos, archived)
    data = tmp_path / "data"
    cfg = {"manifest": str(data / "manifest.db"), "log_dir": str(data / "logs"), "reports_dir": str(data / "reports"),
           "phase2": {"enabled": True, "batch_size": 50},
           "photoprism_export": {"originals": str(orig)},
           "tools": {"photoprism": {"url": fake.url, "username": "admin", "password": "pw"}},
           "triage": {"database": str(data / "triage.db"), "log_dir": str(data / "tlogs"),
                      "reports_dir": str(data / "treports"), "sources": [{"name": "s", "path": str(tmp_path / "src")}]}}
    (tmp_path / "src").mkdir()
    path = tmp_path / "imagepipe.yml"
    path.write_text(yaml.safe_dump(cfg))
    return load_config(path), stack, orig, fake


def as_list(v):
    return v if isinstance(v, list) else [v]


def read(path: Path) -> dict:
    out = subprocess.run(["exiftool", "-j", "-struct", "-G1", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)[0]


def test_plan_rules():
    c = PPExportConfig()
    p = photo("x", [], Title="T", TitleSrc="openai", Caption="C", CaptionSrc="manual", TakenSrc="estimate",
              TakenAtLocal="2020-01-01T00:00:00Z", Private=True, DeletedAt="2020", Favorite=True)
    plan = plan_photo(p, c)
    assert "XMP-dc:Title" not in plan["tags"] and plan["tags"]["XMP-dc:Description"] == "C"
    assert "EXIF:DateTimeOriginal" not in plan["tags"] and plan["tags"]["XMP:Rating"] == 5
    assert plan["keywords"] == ["Favourite", "Review", "Private"]
    f = {"Markers": [{"Type": "face", "Name": "A", "X": 0, "Y": 0, "W": 0.5, "H": 0.5, "SubjSrc": "marker"}]}
    assert faces_for(f, c)[0]["Area"] == {"X": 0.25, "Y": 0.25, "W": 0.5, "H": 0.5, "Unit": "normalized"}
    assert faces_for(f, PPExportConfig(faces="manual")) == []


@needs_exiftool
def test_export(tmp_path, monkeypatch):
    cfg, stack, orig, fake = setup(tmp_path, monkeypatch)
    try:
        before = {p: p.read_bytes() for p in orig.rglob("*") if p.is_file()}
        dry = run_export(cfg, dry_run=True)
        assert dry["stats"]["would-write"] == 2 and not (stack / "imagepipe" / "intake").exists()
        res = run_export(cfg)
        assert res["status"] == "finished"
        assert {p: p.read_bytes() for p in orig.rglob("*") if p.is_file()} == before   # originals untouched
        intake = stack / "imagepipe" / "intake" / "photoprism" / "batch_01" / "trip"
        assert sorted(p.name for p in intake.iterdir()) == ["a.jpg", "b.jpg", "live.jpg", "live.mov"]
        tri = stack / "triage" / "intake" / "photoprism"
        assert sorted(str(p.relative_to(tri)) for p in tri.rglob("*") if p.is_file()) == ["batch_01/trip/notes.txt"]
        a = read(intake / "a.jpg")
        assert a["XMP-dc:Title"] == "Beach day" and "XMP-dc:Description" not in a      # AI caption ignored
        assert set(a["XMP-dc:Subject"]) == {"sand", "sea", "Favourite", "Holidays"}
        assert a["XMP-xmp:Rating"] == 5
        assert as_list(a["XMP-lr:HierarchicalSubject"]) == ["Albums|Holidays"]
        assert as_list(a["XMP-digiKam:TagsList"]) == ["Albums/Holidays"]
        assert a["ExifIFD:DateTimeOriginal"] == "2020:01:02 10:00:00" and a["ExifIFD:OffsetTimeOriginal"] == "+10:00"
        assert "GPS:GPSLatitude" not in a                                                 # estimated place ignored
        names = [r["Name"] for r in a["XMP-mwg-rs:RegionInfo"]["RegionList"]]
        assert names == ["Tonia", "Bob"]
        b = read(intake / "b.jpg")
        assert b["XMP-dc:Description"] == "My words" and as_list(b["XMP-dc:Subject"]) == ["Review"]
        assert b["GPS:GPSLatitudeRef"] == "South"
        assert (intake / "live.mov").read_bytes() == b"moov fake clip"
        # a second run finds everything done
        again = run_export(cfg)
        assert again["stats"].get("already-done", 0) >= 4 and "written" not in again["stats"]
        # phase 2 takes the PhotoPrism batch first, Live clip travels with its photo
        other = stack / "sorted" / "images" / "x"
        other.mkdir(parents=True)
        (other / "z.jpg").write_bytes(jpeg(color=(50, 60, 70), date="2001:01:01 00:00:00"))
        first = run_phase2(cfg)["batch"]
        second = run_phase2(cfg)["batch"]
        assert first == "pp-2019-05" and second == "pp-2020-01"
        m = Manifest(cfg.manifest, cfg.tz)
        assert m.db.execute("SELECT COUNT(*) FROM files WHERE source='intake/photoprism'").fetchone()[0] == 3
        m.close()
        clips = list((stack / "imagepipe" / "processing").rglob("*.mov"))
        stills = [p for p in clips[0].parent.iterdir() if p.suffix == ".jpg" and "live" in p.name]
        assert len(clips) == 1 and len(stills) == 1 and clips[0].stem == stills[0].stem   # renamed with its photo
        assert not list((stack / "imagepipe" / "intake" / "photoprism").rglob("*.jpg"))
        assert run_phase2(cfg)["batch"] == "2001-01"
    finally:
        fake.close()
