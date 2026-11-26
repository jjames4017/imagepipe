# ImagePipe

A repeatable, never-delete photo ingest pipeline. Drop photos (and PDF scans of
photos) into a funnel; ImagePipe catalogues them, removes duplicates, sorts out
documents, screenshots and junk, fixes dates and locations, tags them, and hands
a clean library to PhotoPrism and Immich. Every new drop is checked against
everything that came before.

**Nothing is ever deleted.** Every file ImagePipe takes out of the flow is moved
to an `/excluded/<reason>/` folder and logged with its original path.

## Status

Version 0.2: the foundation, **Catalogue** mode (a read-only inventory of a
folder) and **Triage** (sorts mixed folder trees - photos, documents, archives
and everything else - into the pipeline's inbox, a document staging folder,
sorted type folders or quarantine, renaming each file and logging every
action; see [docs/stages/triage.md](docs/stages/triage.md)) are built. The pipeline stages are being added in phases - see
[docs/roadmap.md](docs/roadmap.md).

## Quick start

```bash
cp config.example.yml config/imagepipe.yml       # edit it
cp compose.example.yml compose.yml               # edit the volume paths
docker compose build
docker compose run --rm imagepipe catalogue --source photos
```

A web page for settings, running jobs and reviewing results: `imagepipe serve`,
then open port 8020 - see [docs/web-ui.md](docs/web-ui.md).

Full documentation is in [`docs/`](docs/index.md) (build it with `mkdocs serve`).

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
