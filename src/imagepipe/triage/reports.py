"""Triage reports: the dry-run plan, apply summaries, status and a full CSV export."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from imagepipe.config import Config
from imagepipe.progress import fmt_bytes
from imagepipe.triage import db as tdb

PLAN_COLUMNS = ("triage_id", "source", "origin", "kind", "size", "bucket", "rule", "companion_of",
                "name_date", "date_source", "smartname", "smartname_source", "dest_path", "sha256")


def _table(rows, headers) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def write_plan_report(cfg: Config, t: tdb.TriageDB, run_id: int) -> Path:
    tc = cfg.triage
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    out = tc.reports_dir / f"plan-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    db = t.db
    where = f"f.status='{tdb.PLANNED}'"

    with open(out / "plan.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(PLAN_COLUMNS)
        cur = db.execute(f"""
            SELECT f.triage_id, f.source, f.origin, f.kind, f.size, f.bucket, f.rule, p.triage_id AS companion_of,
                   f.name_date, f.date_source, f.smartname, f.smartname_source, f.dest_path, f.sha256
            FROM files f LEFT JOIN files p ON p.id = f.companion_of WHERE {where} ORDER BY f.source, f.logical_path""")
        for r in cur:
            w.writerow([r[c] for c in PLAN_COLUMNS])

    lines = [f"# Triage plan ({stamp})", "",
             "Nothing has been moved. Review plan.csv, then run `imagepipe triage apply`.", ""]
    total = db.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files f WHERE {where}").fetchone()
    lines += [f"**{total[0]:,} files planned ({fmt_bytes(total[1])}).**", "", "## Where they go", ""]
    rows = db.execute(f"SELECT bucket, COUNT(*), SUM(size) FROM files f WHERE {where} GROUP BY bucket "
                      "ORDER BY COUNT(*) DESC").fetchall()
    lines += _table([(b, f"{n:,}", fmt_bytes(s or 0)) for b, n, s in rows], ("Bucket", "Files", "Size"))
    lines += ["", "Archives (bucket `extract`) are extracted during apply; their contents are planned and moved",
              "then, and the archive is deleted only after every file inside has been checked.", "",
              "## Why (rules)", ""]
    rows = db.execute(f"SELECT bucket, rule, COUNT(*) FROM files f WHERE {where} GROUP BY bucket, rule "
                      "ORDER BY bucket, COUNT(*) DESC").fetchall()
    lines += _table([(b, r, f"{n:,}") for b, r, n in rows], ("Bucket", "Rule", "Files"))
    lines += ["", "## Where the dates came from", ""]
    rows = db.execute(f"SELECT date_source, COUNT(*) FROM files f WHERE {where} AND companion_of IS NULL "
                      "GROUP BY date_source ORDER BY COUNT(*) DESC").fetchall()
    lines += _table([(s, f"{n:,}") for s, n in rows], ("Date source", "Files"))
    lines += ["", "## Where the smartnames came from", ""]
    rows = db.execute(f"SELECT smartname_source, COUNT(*) FROM files f WHERE {where} AND companion_of IS NULL "
                      "GROUP BY smartname_source ORDER BY COUNT(*) DESC").fetchall()
    lines += _table([(s, f"{n:,}") for s, n in rows], ("Smartname source", "Files"))
    lines += ["", "## Examples", ""]
    rows = db.execute(f"""SELECT origin, dest_path FROM files f WHERE {where} AND dest_path IS NOT NULL
                          ORDER BY random() LIMIT 25""").fetchall()
    lines += _table([(o, d) for o, d in rows], ("From", "To"))
    lines += ["", "## Files read with problems", ""]
    n = db.execute(f"SELECT COUNT(*) FROM files f WHERE {where} AND catalogue_error IS NOT NULL").fetchone()[0]
    lines.append(f"{n:,} files had a problem when read (see the catalogue_error column in export-csv).")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def write_apply_report(cfg: Config, result: dict) -> Path:
    tc = cfg.triage
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    path = tc.reports_dir / f"apply-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = result.get("stats", {})
    lines = [f"# Triage apply ({stamp}) - {result.get('status')}", ""]
    lines += _table(sorted(stats.items()), ("What", "Count"))
    lines += ["", f"Still planned: {result.get('still_planned', 0):,}", "",
              f"Every action is in {tc.reports_dir / 'actions.csv'} and the triage database."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def status_text(cfg: Config) -> str:
    tc = cfg.triage
    t = tdb.TriageDB(tc.database, cfg.tz)
    try:
        lines = [f"Triage database: {tc.database}"]
        for s in tc.sources:
            lines.append(f"Source '{s.name}': {s.path}")
        rows = t.db.execute("SELECT status, COUNT(*), COALESCE(SUM(size), 0) FROM files GROUP BY status").fetchall()
        lines.append("Files by status:")
        lines += [f"  {st:<11} {n:>12,}  {fmt_bytes(b)}" for st, n, b in rows] or ["  (none yet)"]
        rows = t.db.execute(f"SELECT bucket, COUNT(*) FROM files WHERE status IN ('{tdb.DONE}', '{tdb.PLANNED}') "
                            "GROUP BY bucket ORDER BY bucket").fetchall()
        if rows:
            lines.append("Planned or done, by bucket:")
            lines += [f"  {b:<28} {n:>12,}" for b, n in rows]
        rows = t.db.execute("SELECT verdict, state, COUNT(*) FROM pdf_checks GROUP BY verdict, state "
                            "ORDER BY verdict, state").fetchall()
        if rows:
            lines.append("PDF check (verdict / state):")
            lines += [f"  {v or '-':<12} {st:<15} {n:>10,}" for v, st, n in rows]
        lines.append("Recent runs:")
        for r in t.db.execute("SELECT id, mode, source, started_at, finished_at, status FROM runs "
                              "ORDER BY id DESC LIMIT 5"):
            lines.append(f"  #{r['id']} {r['mode']} {r['source'] or ''} {r['started_at']} -> "
                         f"{r['finished_at'] or '...'} [{r['status']}]")
        return "\n".join(lines)
    finally:
        t.close()


def export_csv(cfg: Config, output: Path | None = None) -> Path:
    tc = cfg.triage
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    output = output or tc.reports_dir / f"triage-files-{stamp}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    t = tdb.TriageDB(tc.database, cfg.tz)
    try:
        cur = t.db.execute("SELECT * FROM files ORDER BY source, logical_path")
        cols = [d[0] for d in cur.description]
        with open(output, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in cur:
                w.writerow(list(r))
    finally:
        t.close()
    return output
