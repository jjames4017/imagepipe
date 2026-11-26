# Development

## Layout

```text
src/imagepipe/
  cli.py         command line
  config.py      config loading and validation
  manifest.py    manifest database schema and helpers
  catalogue.py   Catalogue mode
  detect.py      file type detection from contents
  metadata.py    dimensions / EXIF / archive listing
  exiftool.py    persistent ExifTool process
  progress.py    live progress display
  reports.py     reports and CSV export
  notify.py      Apprise notifications
tests/           pytest suite with generated sample files
docs/            this documentation (MkDocs Material)
```

## Running locally

```bash
pip install -e ".[heif,dev]"
```

```bash
pytest -q
```

```bash
mkdocs serve
```

## Rules for contributions

- **Never delete, never modify a source.** Any new stage moves files to an
  excluded folder and logs why.
- **Progress for anything slow.** Anything that can take more than ~30 seconds
  uses `Progress`, shows a rate and an ETA where the total is known, and covers
  every phase, including counting/scanning before the real work starts.
- **Config, not code.** Paths, thresholds, keywords and service URLs belong in
  the config file.
- **Test with generated files.** The test set must never contain personal
  photos, so the repository can be shared.
- **Document each stage** with a page under `docs/stages/`: what it does, inputs
  and outputs, dependencies with versions, config keys, which excluded folders
  it writes to, and how it is tested.

## Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs the tests on Python 3.11-3.13,
builds the docs in strict mode, and on `main` publishes the Docker image to the
GitHub Container Registry.
