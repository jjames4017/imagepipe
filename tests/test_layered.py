"""Layered TIFFs and Photoshop files: review list and working copies (#51)."""

import csv
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

np = pytest.importorskip("numpy")
tifffile = pytest.importorskip("tifffile")
psd_tools = pytest.importorskip("psd_tools")

from imagepipe import layered  # noqa: E402


def make_psd(path: Path, layers=2):
    psd = psd_tools.PSDImage.new("RGB", (64, 48))
    for i in range(layers):
        psd.create_pixel_layer_from_array(np.full((24, 32, 3), (200, 40 * i, 10), dtype=np.uint8),
                                          name=f"L{i}", top=i * 10, left=i * 10)
    psd.save(str(path))


def make_tiffs(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(folder / "layered.tif", (np.random.rand(40, 50, 4) * 65535).astype("uint16"),
                     photometric="separated", compression="lzw", extratags=[(37724, "B", 4, b"Adob", False)])
    tifffile.imwrite(folder / "zstd.tif", np.full((20, 30, 3), 120, dtype=np.uint8), photometric="rgb",
                     compression="jpeg2000")  # an encoding Pillow can't read
    Image.new("RGB", (30, 20), (9, 9, 9)).save(folder / "plain.tif")


def test_inspect_psd_and_tiffs(tmp_path):
    make_psd(tmp_path / "poster.psd")
    make_tiffs(tmp_path)
    p = layered.inspect(str(tmp_path / "poster.psd"))
    assert (p["type"], p["why_listed"], p["layers"], p["has_merged_image"]) == ("psd", "photoshop", 2, "yes")
    assert (p["width"], p["height"], p["colour_mode"], p["bit_depth"]) == (64, 48, "RGB", 8)
    t = layered.inspect(str(tmp_path / "layered.tif"))
    assert (t["why_listed"], t["colour_mode"], t["bit_depth"], t["width"]) == ("layered", "CMYK", 16, 50)
    z = layered.inspect(str(tmp_path / "zstd.tif"))
    assert z["why_listed"] == "pillow-cannot-read" and z["pillow_can_read"] == "no"
    assert layered.inspect(str(tmp_path / "plain.tif"))["why_listed"] == ""
    (tmp_path / "broken.psd").write_bytes(b"8BPS" + b"\x00" * 4)
    assert layered.inspect(str(tmp_path / "broken.psd"))["note"]  # never raises


def test_find_writes_review_report(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    make_psd(src / "a" / "poster.psd")
    shutil.copy(src / "a" / "poster.psd", src / "a" / "poster copy.psd")
    make_tiffs(src / "b")
    (src / "b" / "photo.jpg").write_bytes(b"not looked at")
    res = layered.find([src], tmp_path / "reports", quiet=True)
    assert res["looked_at"] == 5 and res["listed"] == 4 and res["unique"] == 3
    rows = list(csv.DictReader(open(Path(res["folder"]) / "layered.csv", encoding="utf-8")))
    names = {Path(r["path"]).name: r for r in rows}
    assert set(names) == {"poster.psd", "poster copy.psd", "layered.tif", "zstd.tif"}
    assert names["poster.psd"]["copies"] == "2" and names["layered.tif"]["copies"] == "1"
    summary = (Path(res["folder"]) / "summary.md").read_text()
    assert "Listed: 4 files" in summary and "3 different" in summary


def test_render_working_copies(tmp_path):
    make_psd(tmp_path / "poster.psd")
    make_tiffs(tmp_path)
    out = tmp_path / "out"
    dest, how = layered.render(str(tmp_path / "poster.psd"), out)
    assert dest.name == "poster.jpg" and how == "psd merged image"
    with Image.open(dest) as im:
        assert im.size == (64, 48) and im.mode == "RGB"
    dest, how = layered.render(str(tmp_path / "layered.tif"), out)  # 16-bit CMYK -> 8-bit RGB
    with Image.open(dest) as im:
        assert im.size == (50, 40) and im.mode == "RGB"
    dest, how = layered.render(str(tmp_path / "zstd.tif"), out)
    assert how == "tiff (tifffile)"
    with Image.open(dest) as im:
        assert im.size == (30, 20) and im.getpixel((5, 5))[0] in range(110, 131)


def test_cli(tmp_path, capsys):
    from imagepipe.cli import build_parser, cmd_layered_find, cmd_layered_render

    src = tmp_path / "src"
    src.mkdir()
    make_psd(src / "poster.psd")
    cfg = SimpleNamespace(reports_dir=tmp_path / "reports", tz=None, sources=[SimpleNamespace(name="vault", path=src)],
                          source=lambda n: SimpleNamespace(name=n, path=src))
    args = build_parser().parse_args(["layered", "find", "--source", "vault"])
    assert cmd_layered_find(cfg, args) == 0
    assert "Listed: 1 files" in capsys.readouterr().out
    args = build_parser().parse_args(["layered", "find", "--path", str(tmp_path / "nope")])
    assert cmd_layered_find(cfg, args) == 2
    args = build_parser().parse_args(["layered", "render", str(src / "poster.psd"), "--out", str(tmp_path / "r")])
    assert cmd_layered_render(cfg, args) == 0 and (tmp_path / "r" / "poster.jpg").exists()


def test_move_listed_keeps_folders_and_follows_in_triage(tmp_path):
    import sqlite3

    root = tmp_path / "vault" / "imagepipe"
    (root / "file cabinet" / "a").mkdir(parents=True)
    make_psd(root / "file cabinet" / "a" / "poster.psd")
    make_tiffs(root / "file cabinet" / "b")
    res = layered.find([root], tmp_path / "reports", quiet=True)
    csv_path = Path(res["folder"]) / "layered.csv"
    to = tmp_path / "vault" / "sorted" / "specials"

    dry = layered.move_listed(csv_path, root, to, tmp_path / "reports", dry_run=True)
    assert dry["would-move"] == 3 and (root / "file cabinet" / "a" / "poster.psd").exists() and not to.exists()

    class FakeT:  # the bits of TriageDB that move_listed uses
        def __init__(self, path):
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, current_path, dest_path, bucket, rule, "
                            "triage_id)")
            self.db.execute("INSERT INTO files (current_path, bucket, triage_id) VALUES (?, 'imagepipe', 'IPX')",
                            (str(root / "file cabinet" / "a" / "poster.psd"),))
            self.actions = []

        def update(self, fid, **cols):
            sets = ", ".join(f"{k}=?" for k in cols)
            self.db.execute(f"UPDATE files SET {sets} WHERE id=?", (*cols.values(), fid))

        def action(self, run_id, row, action, from_path, to_path, rule, detail=""):
            self.actions.append((action, from_path, to_path))

    t = FakeT(tmp_path / "t.db")
    res = layered.move_listed(csv_path, root, to, tmp_path / "reports", triage_db=t)
    assert res["moved"] == 3 and "error" not in res
    assert (to / "file cabinet" / "a" / "poster.psd").exists()
    assert (to / "file cabinet" / "b" / "layered.tif").exists() and (to / "file cabinet" / "b" / "zstd.tif").exists()
    assert not (root / "file cabinet" / "a").exists()  # emptied folders removed
    assert (root / "file cabinet" / "b" / "plain.tif").exists()  # not on the list: stays
    row = t.db.execute("SELECT * FROM files").fetchone()
    assert row["current_path"] == str(to / "file cabinet" / "a" / "poster.psd") and row["bucket"] == "specials"
    assert t.actions and t.actions[0][0] == "moved"
    again = layered.move_listed(csv_path, root, to, tmp_path / "reports")
    assert again["missing"] == 3  # already moved: skipped, nothing overwritten
