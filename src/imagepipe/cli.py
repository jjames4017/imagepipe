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
            result = run_scan(cfg, source)
        except TriageAbort as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            log.error(str(exc))
            notify.send(cfg.notify, "error", f"Triage scan stopped: {source.name}", str(exc), "failure")
            return 2
        body = (f"{result.get('read', 0):,} files read, {result.get('read_problems', 0):,} with problems, "
                f"{result.get('missing', 0):,} missing. Scanned {result.get('scanned', 0):,} files "
                f"({fmt_bytes(result.get('scanned_bytes', 0))}).")
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

    t = sub.add_parser("triage", help="sort mixed folder trees: scan, plan (dry run), apply")
    tsub = t.add_subparsers(dest="triage_command", required=True)
    ts = tsub.add_parser("scan", help="read-only inventory of the triage sources")
    ts.add_argument("--source", help="one triage source (default: all)")
    ts.set_defaults(func=cmd_triage_scan)
    tp = tsub.add_parser("plan", help="decide destinations and names; writes a dry-run report, moves nothing")
    tp.set_defaults(func=cmd_triage_plan)
    ta = tsub.add_parser("apply", help="carry out the plan: move, extract archives, write tags")
    ta.add_argument("--limit", type=int, help="stop after this many files/archives (for a trial run)")
    ta.set_defaults(func=cmd_triage_apply)
    tst = tsub.add_parser("status", help="counts by status and bucket, recent runs")
    tst.set_defaults(func=cmd_triage_status)
    te = tsub.add_parser("export-csv", help="write every file in the triage database to one CSV")
    te.add_argument("--output")
    te.set_defaults(func=cmd_triage_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
