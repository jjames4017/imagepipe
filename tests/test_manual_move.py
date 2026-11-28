"""Files tab: search and move triage files by hand."""

from __future__ import annotations

import pytest

from imagepipe.fspath import to_db
from imagepipe.triage import manual
from tests.test_dedupe import env  # noqa: F401 - the same fixture


def test_search_and_move(env):  # noqa: F811
    c, t, add, stack = env
    pid, ppath = add("documents", "Family/letters/note.pdf", content=b"pdf")
    side_id, side = add("documents", "Family/letters/note.pdf.xmp", content=b"x", ext="xmp", companion_of=pid,
                        fname=ppath.name + ".xmp")
    t.db.execute("INSERT INTO pdf_checks (path, verdict, state, final_path) VALUES (?, 'uncertain', 'done', ?)",
                 (to_db(str(ppath)), to_db(str(ppath))))
    t.db.commit()
    dests = manual.destinations(c.triage)
    assert "documents" in dests and "quarantine/pdf-uncertain" in dests and "excluded/triage-duplicate" in dests
    found = manual.search(c, "letters", "documents")
    assert found["total"] == 1 and found["rows"][0]["where"] == "documents"
    res = manual.move_files(c, [pid], "quarantine/pdf-uncertain")
    assert res["moved"] == 1
    new = stack / "sorted/quarantine/pdf-uncertain/src/Family/letters" / ppath.name   # source folder + sub-folders
    assert new.exists() and new.with_name(ppath.name + ".xmp").exists() and not ppath.exists()
    t.db.commit()
    row = t.db.execute("SELECT dest_path, bucket FROM files WHERE id=?", (pid,)).fetchone()
    assert row[0] == str(new) and row[1] == "quarantine/pdf-uncertain"
    pc = t.db.execute("SELECT verdict, state FROM pdf_checks WHERE path=?", (to_db(str(new)),)).fetchone()
    assert tuple(pc) == ("manual", "done")              # Check PDFs leaves it alone
    assert manual.search(c, "", "quarantine/pdf-uncertain")["total"] == 1
    again = manual.move_files(c, [pid], "quarantine/pdf-uncertain")
    assert again["moved"] == 0 and "already in" in again["skipped"][0]["why"]
    with pytest.raises(ValueError):
        manual.move_files(c, [pid], "nowhere")
