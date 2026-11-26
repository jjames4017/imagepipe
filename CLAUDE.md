# ImagePipe - notes for Claude

ImagePipe is a repeatable, never-delete photo ingest pipeline (GPL-3.0). It
runs on the owner's home-lab host **amd9** as the `imagepipe` container. Read
`docs/design.md` (rules and decisions) and `docs/roadmap.md` before changing
behaviour; when code and `docs/design.md` disagree, the doc is the intent.

## Layout

- `src/imagepipe/` - `cli.py` (all commands), `config.py` (YAML loader; unknown keys are errors),
  `catalogue.py` + `manifest.py` (read-only inventory, SQLite manifest), `detect.py` (type from contents),
  `ids.py` (pipeline IDs: `IP` + 16 Crockford base32), `fspath.py` (non-UTF-8 names), `progress.py`, `notify.py` (Apprise)
- `src/imagepipe/triage/` - scan -> plan (dry run) -> apply; own SQLite DB; see `docs/stages/triage.md`
- `src/imagepipe/web/` - `imagepipe serve` web UI (stdlib only); `schema.py` describes every config key for the
  settings form - **add new config keys there too**
- `tests/` - generated fixtures only (never personal files); `pytest -q`

## Rules the code must keep

- Never delete an end-point file (photo, PDF, document). Exclusions are moved and logged. The only deletions
  are an archive after every extracted file is verified, and triage's own temp files.
- Sources are only read by catalogue/scan. Moves: hard link + inode check (same FS) or copy + SHA-256 (across FS);
  never overwrite; interrupted moves are recovered on the next run.
- **One bad file never stops a run**: record it against that file (status `error` + action log) and carry on.
  Only whole-run problems (unmounted source, unwritable destination, no space) abort.
- Every phase that can take more than ~30 s shows live progress on stderr (count, rate, and % / ETA once the
  total is known) via `progress.Progress`. Never hide progress. `IMAGEPIPE_PROGRESS_INTERVAL` sets the redraw rate.
- Every action is recorded (triage: `actions` table + `reports/actions.csv`).
- Paths stored in SQLite go through `fspath.to_db()`; paths used on disk through `fspath.to_fs()`.
- Triage writes nothing into files by default; the ID is in the file name. ImagePipe stage 2 writes it in.
- Time zone default: Australia/Brisbane.

## Workflow

- Work on a branch and open a pull request; merge only when the owner says so.
- Before pushing: `pytest -q`, `python -m pyflakes src tests`, `mkdocs build --strict`.
- Bump the version in both `pyproject.toml` and `src/imagepipe/__init__.py`, and update the docs page for any
  behaviour change.
- Merging to main runs CI (tests on 3.11-3.13, docs) and pushes `ghcr.io/jjames4017/imagepipe:latest`.
- Feature requests and fixes for "the next version" are GitHub issues on this repo.

## Deployment (amd9)

- Compose `/etc/wolfstack/compose/imagepipe/docker-compose.yml`; config `/appdata/imagepipe/imagepipe.yml`
- Runs as 1000:1000, `network_mode: host`, `command: ["serve"]` -> web UI http://10.10.1.30:8020/
- Mounts: `/appdata/imagepipe` -> `/config` (rw); `/mnt/appdata/photostage/imagepipe` -> `/data` and at its host
  path; `/wolf-pool/vault` at the same path (rw, single ZFS dataset)
- Update: `docker compose pull && docker compose up -d`
