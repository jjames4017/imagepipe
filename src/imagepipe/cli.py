"""Command line interface: `imagepipe <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from imagepipe import __version__, notify
from imagepipe.config import ConfigError, load_config
from imagepipe.progress import fmt_bytes

log = logging.getLogger("imagepipe")


def _setup_logging(cfg) -> Path:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.log_dir / f"imagepipe-{datetime.now(cfg.tz):%Y%m%d}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return path


def _pick_source(cfg, name: str | None):
    if name:
        return cfg.source(name)
    if len(cfg.sources) == 1:
        return cfg.sources[0]
    names = ", ".join(s.name for s in cfg.sources) or "(none configured)"
    raise ConfigError(f"Choose a source with --source. Configured sources: {names}")


def cmd_catalogue(cfg, args) -> int:
    from imagepipe.catalogue import CatalogueAbort, run_catalogue
    from imagepipe.reports import write_reports

    source = _pick_source(cfg, args.source)
    try:
        result = run_catalogue(cfg, source.name, retry_errors=args.retry_errors)
    except CatalogueAbort as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        log.error(str(exc))
        notify.send(cfg.notify, "error", f"ImagePipe catalogue stopped: {source.name}", str(exc), "failure")
        return 2
    except Exception as exc:
        log.exception("Catalogue failed")
        notify.send(cfg.notify, "error", f"ImagePipe catalogue failed: {source.name}", repr(exc), "failure")
        raise

    print("Writing reports...", file=sys.stderr, flush=True)
    out = write_reports(cfg, source)
    print((out / "summary.md").read_text(encoding="utf-8"))
    print(f"Reports written to: {out}", file=sys.stderr)

    body = (f"{result.get('read', 0):,} files read, {result.get('read_errors', 0):,} errors, "
            f"{result.get('missing', 0):,} missing. Scanned {result.get('scanned', 0):,} files "
            f"({fmt_bytes(result.get('scanned_bytes', 0))}).")
    if result["status"] == "finished":
        notify.send(cfg.notify, "finished", f"ImagePipe catalogue finished: {source.name}", body, "success")
        return 0
    notify.send(cfg.notify, "error", f"ImagePipe catalogue interrupted: {source.name}", body, "warning")
    return 130


def cmd_report(cfg, args) -> int:
    from imagepipe.reports import write_reports

    out = write_reports(cfg, _pick_source(cfg, args.source))
    print((out / "summary.md").read_text(encoding="utf-8"))
    print(f"Reports written to: {out}", file=sys.stderr)
    return 0


def cmd_export_csv(cfg, args) -> int:
    from imagepipe.reports import export_csv

    out = export_csv(cfg, _pick_source(cfg, args.source), Path(args.output) if args.output else None)
    print(f"CSV written to: {out}", file=sys.stderr)
    return 0


def cmd_status(cfg, args) -> int:
    from imagepipe.manifest import Manifest

    m = Manifest(cfg.manifest, cfg.tz)
    print(f"ImagePipe {__version__}")
    print(f"Config:   {cfg.path}")
    print(f"Manifest: {cfg.manifest}")
    for s in cfg.sources:
        row = m.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0), SUM(status = 'missing') FROM files WHERE source=?", (s.name,)
        ).fetchone()
        print(f"Source '{s.name}': {s.display_path('')} - {row[0]:,} files ({fmt_bytes(row[1])}), "
              f"{row[2] or 0:,} missing")
    print("Recent runs:")
    for r in m.db.execute("SELECT id, mode, source, started_at, finished_at, status FROM runs ORDER BY id DESC LIMIT 5"):
        print(f"  #{r['id']} {r['mode']} {r['source']} {r['started_at']} -> {r['finished_at'] or '...'} [{r['status']}]")
    m.close()
    return 0


def cmd_notify_test(cfg, args) -> int:
    if not cfg.notify.enabled:
        print("Notifications are not configured (notify.apprise_url and notify.apprise_key).", file=sys.stderr)
        return 1
    events = cfg.notify.events
    cfg.notify.events = ["test"]
    ok = notify.send(cfg.notify, "test", "ImagePipe test", "If you can read this, notifications work.", "info")
    cfg.notify.events = events
    print("Sent." if ok else "Failed - see the log for the reason.", file=sys.stderr)
    return 0 if ok else 1


# ---------------------------------------------------------------- triage
def _triage_cfg(cfg):
    if cfg.triage is None:
        raise ConfigError("There is no 'triage:' section in the config file. See config.example.yml.")
    tc = cfg.triage
    tc.log_dir.mkdir(parents=True, exist_ok=True)
    path = tc.log_dir / f"triage-{datetime.now(cfg.tz):%Y%m%d}.log"
    if not any(getattr(h, "baseFilename", None) == str(path) for h in log.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
    return tc


def cmd_triage_scan(cfg, args) -> int:
    from imagepipe.triage.scan import TriageAbort, run_scan

    tc = _triage_cfg(cfg)
    sources = [tc.source(args.source)] if args.source else tc.sources
    code = 0
    for source in sources:
        try:
            result = run_scan(cfg, source, reread="ext" if args.reread_ext else args.reread,
                              reread_ext=[args.reread_ext] if args.reread_ext else None)
        except TriageAbort as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            log.error(str(exc))
            notify.send(cfg.notify, "error", f"Triage scan stopped: {source.name}", str(exc), "failure")
            return 2
        body = (f"{result.get('read', 0):,} files read, {result.get('read_problems', 0):,} with problems, "
                f"{result.get('missing', 0):,} missing. Scanned {result.get('scanned', 0):,} files "
                f"({fmt_bytes(result.get('scanned_bytes', 0))}).")
        if result.get("exif_damaged"):
            body += (f" {result['exif_damaged']:,} images have damaged EXIF (harmless: the images are fine, "
                     f"some EXIF details such as the date may be missing).")
        print(f"Source '{source.name}': {body}", file=sys.stderr)
        if result["status"] != "finished":
            notify.send(cfg.notify, "error", f"Triage scan interrupted: {source.name}", body, "warning")
            return 130
        notify.send(cfg.notify, "finished", f"Triage scan finished: {source.name}", body, "success")
    print("Next: imagepipe triage plan", file=sys.stderr)
    return code


def cmd_triage_plan(cfg, args) -> int:
    from imagepipe.triage.plan import run_plan

    _triage_cfg(cfg)
    result = run_plan(cfg)
    if result["status"] != "finished":
        return 130
    out = Path(result["report"])
    print((out / "summary.md").read_text(encoding="utf-8"))
    print(f"Plan written to: {out} (plan.csv lists every file)", file=sys.stderr)
    return 0


def cmd_triage_apply(cfg, args) -> int:
    from imagepipe.triage.apply import run_apply
    from imagepipe.triage.reports import write_apply_report
    from imagepipe.triage.scan import TriageAbort

    tc = _triage_cfg(cfg)
    try:
        result = run_apply(cfg, limit=args.limit)
    except TriageAbort as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        log.error(str(exc))
        notify.send(cfg.notify, "error", "Triage apply stopped", str(exc), "failure")
        return 2
    except Exception as exc:
        log.exception("Triage apply failed")
        notify.send(cfg.notify, "error", "Triage apply failed", repr(exc), "failure")
        raise
    path = write_apply_report(cfg, result)
    print(path.read_text(encoding="utf-8"))
    print(f"Report: {path}  |  Action log: {tc.reports_dir / 'actions.csv'}", file=sys.stderr)
    stats = result.get("stats", {})
    body = (f"{sum(v for k, v in stats.items() if k.startswith('moved:')):,} files moved, "
            f"{stats.get('archives-extracted', 0):,} archives extracted, {stats.get('errors', 0):,} errors, "
            f"{result.get('still_planned', 0):,} still planned.")
    if result["status"] == "finished":
        notify.send(cfg.notify, "finished", "Triage apply finished", body, "success")
        return 0
    notify.send(cfg.notify, "error", "Triage apply interrupted", body, "warning")
    return 130


def cmd_triage_check_pdfs(cfg, args) -> int:
    from imagepipe.triage.pdfcheck import run_pdf_check

    tc = _triage_cfg(cfg)
    try:
        result = run_pdf_check(cfg, limit=args.limit)
    except Exception as exc:
        log.exception("PDF check failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        notify.send(cfg.notify, "error", "Triage PDF check failed", str(exc), "failure")
        return 2
    stats = result["stats"]
    print("PDF check: " + (", ".join(f"{v:,} {k}" for k, v in sorted(stats.items())) or "nothing to do"))
    print(f"Every move and export is in {tc.reports_dir / 'actions.csv'}", file=sys.stderr)
    body = ", ".join(f"{v:,} {k}" for k, v in sorted(stats.items())) or "nothing to do"
    if result["status"] == "finished":
        notify.send(cfg.notify, "finished", "Triage PDF check finished", body, "success")
        return 0
    notify.send(cfg.notify, "error", "Triage PDF check interrupted", body, "warning")
    return 130


def cmd_triage_status(cfg, args) -> int:
    from imagepipe.triage.reports import status_text

    _triage_cfg(cfg)
    print(status_text(cfg))
    return 0


def cmd_triage_export(cfg, args) -> int:
    from imagepipe.triage.reports import export_csv

    _triage_cfg(cfg)
    out = export_csv(cfg, Path(args.output) if args.output else None)
    print(f"CSV written to: {out}", file=sys.stderr)
    return 0


def cmd_triage_relocate(cfg, args) -> int:
    from imagepipe.triage.relocate import run_relocate

    tc = _triage_cfg(cfg)
    print(("Dry run - nothing moves.\n" if args.dry_run else "") + "From the old folders:\n" +
          "".join(f"  {k}: {v}\n" for k, v in tc.legacy_destinations.items()) +
          f"to the filestack: {tc.filestack}/sorted/", file=sys.stderr)
    res = run_relocate(cfg, dry_run=args.dry_run)
    print(", ".join(f"{v:,} {k}" for k, v in res.items() if k != "folder") or "nothing to do")
    print(f"Every file is listed in {res['folder']}/moves.csv")
    if res.get("copied"):
        print(f"WARNING: {res['copied']:,} files were copied instead of linked - the old folders and the filestack "
              "are on different mounts in the container. Moves still checked by SHA-256.", file=sys.stderr)
    if not args.dry_run and not res.get("error"):
        print("Next: run `imagepipe triage plan` again (files planned before used the old folders).", file=sys.stderr)
    return 1 if res.get("error") else 0


# ---------------------------------------------------------------- phase 2
def cmd_phase2_run(cfg, args) -> int:
    from imagepipe.phase2.run import Phase2Error, run_phase2

    if cfg.triage is not None:
        _triage_cfg(cfg)
    try:
        result = run_phase2(cfg, batch=args.batch, find_only=args.find_only)
    except Phase2Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        log.exception("Phase 2 failed")
        notify.send(cfg.notify, "error", "ImagePipe phase 2 failed", repr(exc), "failure")
        raise
    stats = result["stats"]
    body = f"Batch {result['batch'] or '(none)'}: " + (", ".join(f"{v:,} {k}" for k, v in sorted(stats.items()))
                                                      or "nothing to do")
    print(body)
    if result["status"] == "finished":
        notify.send(cfg.notify, "finished", "ImagePipe phase 2 finished", body, "success")
        return 0
    notify.send(cfg.notify, "error", "ImagePipe phase 2 interrupted", body, "warning")
    return 130


def cmd_phase2_status(cfg, args) -> int:
    from imagepipe.phase2.run import status_text

    print(status_text(cfg))
    return 0


def cmd_phase2_ready(cfg, args) -> int:
    from imagepipe.phase2.checks import ready_check

    res = ready_check(cfg, args.batch)
    print(f"{res['checked']:,} checked: {res['ready']:,} ready, {res['not_ready']:,} not ready")
    print(f"Report: {res['report']}", file=sys.stderr)
    return 0


def cmd_layered_find(cfg, args) -> int:
    from imagepipe import layered

    roots = [Path(p) for p in (args.path or [])]
    for name in args.source or []:
        roots.append(Path(cfg.source(name).path))
    if not roots:
        roots = [Path(s.path) for s in cfg.sources]
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        print(f"Not a folder: {', '.join(missing)}", file=sys.stderr)
        return 2
    layered.find(roots, cfg.reports_dir / "layered", cfg.tz)
    return 0


def cmd_layered_render(cfg, args) -> int:
    from imagepipe import layered

    out = Path(args.out) if args.out else cfg.reports_dir / "layered" / "renders"
    failed = 0
    for f in args.files:
        try:
            dest, how = layered.render(f, out)
            print(f"{f}\n  -> {dest}  ({how})")
        except layered.MissingDependency as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - report and carry on with the next file
            failed += 1
            print(f"{f}\n  !! could not render: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def cmd_layered_move(cfg, args) -> int:
    from imagepipe import layered

    tc = getattr(cfg, "triage", None)
    root = Path(args.root) if args.root else (tc.bucket_dir("images") if tc else None)
    to = Path(args.to) if args.to else (tc.bucket_dir("specials") if tc else None)
    if root is None or to is None:
        print("Give --root and --to (no triage section configured).", file=sys.stderr)
        return 2
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"No such file: {csv_path}", file=sys.stderr)
        return 2
    t = None
    if tc is not None and not args.dry_run and Path(tc.database).is_file():
        from imagepipe.triage import db as tdb

        t = tdb.TriageDB(tc.database, cfg.tz, tc.reports_dir / "actions.csv")
    print(f"{'Dry run - nothing moves. ' if args.dry_run else ''}From {root}\n  to {to}", file=sys.stderr)
    try:
        res = layered.move_listed(csv_path, root, to, cfg.reports_dir / "layered", cfg.tz, args.dry_run, t)
    finally:
        if t is not None:
            t.close()
    print(", ".join(f"{v:,} {k}" for k, v in res.items() if k != "folder") or "nothing to do")
    print(f"Every file is listed in {res['folder']}/moves.csv")
    return 1 if res.get("error") else 0


def cmd_serve(args) -> int:
    import os

    from imagepipe.config import DEFAULT_CONFIG_PATH
    from imagepipe.web.server import serve

    path = Path(args.config or os.environ.get("IMAGEPIPE_CONFIG") or DEFAULT_CONFIG_PATH)
    return serve(path, args.host, args.port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imagepipe", description="ImagePipe photo pipeline")
    p.add_argument("--version", action="version", version=f"imagepipe {__version__}")
    p.add_argument("-c", "--config", help="config file (default: $IMAGEPIPE_CONFIG or /config/imagepipe.yml)")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("catalogue", help="read-only inventory of a source folder")
    c.add_argument("--source", help="source name from the config")
    c.add_argument("--retry-errors", action="store_true", help="also re-read files that failed last time")
    c.set_defaults(func=cmd_catalogue)

    r = sub.add_parser("report", help="regenerate the catalogue reports")
    r.add_argument("--source")
    r.set_defaults(func=cmd_report)

    e = sub.add_parser("export-csv", help="write every catalogued file to one CSV")
    e.add_argument("--source")
    e.add_argument("--output", help="output file (default: in reports_dir)")
    e.set_defaults(func=cmd_export_csv)

    s = sub.add_parser("status", help="show config, manifest totals and recent runs")
    s.set_defaults(func=cmd_status)

    n = sub.add_parser("notify-test", help="send a test notification through Apprise")
    n.set_defaults(func=cmd_notify_test)

    w = sub.add_parser("serve", help="run the web UI (settings, jobs, reports)")
    w.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    w.add_argument("--port", type=int, default=8020, help="port (default: 8020)")
    w.set_defaults(func=None)

    t = sub.add_parser("triage", help="sort mixed folder trees: scan, plan (dry run), apply")
    tsub = t.add_subparsers(dest="triage_command", required=True)
    ts = tsub.add_parser("scan", help="read-only inventory of the triage sources")
    ts.add_argument("--source", help="one triage source (default: all)")
    rr = ts.add_mutually_exclusive_group()
    rr.add_argument("--reread-problems", dest="reread", action="store_const", const="problems",
                    help="also read again every file that had a read problem last time")
    rr.add_argument("--reread-ext", metavar="TYPES",
                    help="also read again every file of these types, e.g. eml or eml,msg")
    rr.add_argument("--reread-all", dest="reread", action="store_const", const="all",
                    help="read every file again, ignoring the unchanged check")
    ts.set_defaults(func=cmd_triage_scan)
    tp = tsub.add_parser("plan", help="decide destinations and names; writes a dry-run report, moves nothing")
    tp.set_defaults(func=cmd_triage_plan)
    ta = tsub.add_parser("apply", help="carry out the plan: move, extract archives, write tags")
    ta.add_argument("--limit", type=int, help="stop after this many files/archives (for a trial run)")
    ta.set_defaults(func=cmd_triage_apply)
    tc_ = tsub.add_parser("check-pdfs", help="sort the PDFs in sorted/documents into documents, photo scans "
                          "(split by ScanSplitter) and uncertain")
    tc_.add_argument("--limit", type=int, help="check at most this many new PDFs (for a trial run); PDFs waiting "
                     "for review in ScanSplitter are still followed up")
    tc_.set_defaults(func=cmd_triage_check_pdfs)
    tst = tsub.add_parser("status", help="counts by status and bucket, recent runs")
    tst.set_defaults(func=cmd_triage_status)
    tr = tsub.add_parser("relocate", help="move already-sorted files from the old folders (before 0.4) into the filestack")
    tr.add_argument("--dry-run", action="store_true", help="list what would move; move nothing")
    tr.set_defaults(func=cmd_triage_relocate)
    te = tsub.add_parser("export-csv", help="write every file in the triage database to one CSV")
    te.add_argument("--output")
    te.set_defaults(func=cmd_triage_export)

    p2 = sub.add_parser("phase2", help="stages 1, 2 and 4: month batches, IDs, exact duplicates, conflicts")
    p2sub = p2.add_subparsers(dest="phase2_command", required=True)
    pr = p2sub.add_parser("run", help="find new photos, make batches, and take one batch through stages 1, 2 and 4")
    pr.add_argument("--batch", help="this batch (default: the one in progress, else the oldest waiting)")
    pr.add_argument("--find-only", action="store_true", help="only find photos and make batches; move nothing")
    pr.set_defaults(func=cmd_phase2_run)
    ps = p2sub.add_parser("status", help="batches, photos by status, conflicts waiting")
    ps.set_defaults(func=cmd_phase2_status)
    prc = p2sub.add_parser("ready-check", help="what each processed photo still needs before the library")
    prc.add_argument("--batch", help="one batch (default: all)")
    prc.set_defaults(func=cmd_phase2_ready)

    lay = sub.add_parser("layered", help="layered TIFFs and Photoshop files: review list, working copies")
    lsub = lay.add_subparsers(dest="layered_command", required=True)
    lf = lsub.add_parser("find", help="list PSD/PSB files and layered or unreadable TIFFs for review (read-only)")
    lf.add_argument("--source", action="append", help="a catalogue source (repeatable; default: all)")
    lf.add_argument("--path", action="append", help="any folder to search instead (repeatable)")
    lf.set_defaults(func=cmd_layered_find)
    lr = lsub.add_parser("render", help="make a full-resolution JPEG/PNG working copy of PSD/PSB/TIFF files")
    lr.add_argument("files", nargs="+")
    lr.add_argument("--out", help="folder for the working copies (default: <reports_dir>/layered/renders)")
    lr.set_defaults(func=cmd_layered_render)
    lm = lsub.add_parser("move", help="move every file in a 'layered find' CSV aside, keeping its folders")
    lm.add_argument("--csv", required=True, help="the layered.csv from 'imagepipe layered find'")
    lm.add_argument("--to", help="where to (default: <filestack>/sorted/specials)")
    lm.add_argument("--root", help="the folder the paths are under (default: <filestack>/sorted/images)")
    lm.add_argument("--dry-run", action="store_true", help="list what would move; move nothing")
    lm.set_defaults(func=cmd_layered_move)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":  # starts even with a broken config, so it can be fixed in the UI
        return cmd_serve(args)
    try:
        cfg = load_config(args.config)
        _setup_logging(cfg)
        return args.func(cfg, args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
