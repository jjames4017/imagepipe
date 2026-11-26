# ImagePipe

ImagePipe is a repeatable photo ingest pipeline. You drop photos, RAW files and
PDF scans of photo pages into a funnel. Each file moves through a fixed series
of stages, and every new drop is checked against everything that came before.

The end result is one clean library - no duplicates, documents and junk sorted
out, dates and locations fixed, faces named, descriptions written into the
files - ready for PhotoPrism and Immich.

## Three rules

1. **Nothing is ever deleted.** A file taken out of the flow goes to
   `/excluded/<reason>/`, and a CSV log records where it came from and why.
2. **Everything is recorded.** One manifest database knows every file ever seen,
   its fingerprints, the stage it is at and every decision made about it.
3. **Information lives in the file.** Tags, names, captions and locations are
   embedded into the image itself (RAW files are the one exception).

## What is built

| Part | Status |
|---|---|
| Config, manifest database, logging, notifications | Built (0.1) |
| [Catalogue](stages/catalogue.md) - read-only inventory of a folder | Built (0.1) |
| Pipeline stages 1-12 | Planned - see the [roadmap](roadmap.md) |

## Where to go next

- [Quick start](quickstart.md) - run your first catalogue.
- [Architecture](architecture.md) - how the stages fit together.
- [Rules and decisions](design.md) - every design decision and the reason for it.
