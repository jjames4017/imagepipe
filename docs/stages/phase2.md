# Phase 2: stages 1, 2 and 4

Phase 2 takes the photos triage sorted into the categories whose pipeline is
`imagepipe` (by default `sorted/images` and `sorted/raw`) through stage 1 (month
batches), stage 2 (ID check, fingerprint, ID written into the file) and stage 4
(exact duplicates), reads Google Takeout dates and GPS, and sends disagreements
to the **Conflicts** tab. Stage 3 (PDFs) is triage's
[Check PDFs](triage.md#check-pdfs) job.

!!! warning "Switched off until the PhotoPrism export exists"
    `phase2.enabled` is `false` by default and every phase 2 job refuses to run
    until it is switched on (Settings > Phase 2).

## Commands

| Command | What it does | Changes files? |
|---|---|---|
| `imagepipe phase2 run --find-only` | Finds new photos, reads their dates, makes batches | No |
| `imagepipe phase2 run [--batch NAME]` | The above, then one batch through stages 1, 2 and 4 | **Yes - moves and writes files** |
| `imagepipe phase2 status` | Batches, photos by status, conflicts waiting | No |
| `imagepipe phase2 ready-check [--batch NAME]` | What each photo still needs before the library (a CSV report) | No |

Run tab: **Phase 2** - *Find photos, make batches*, *Run batch - moves files*
(asks first) and *Ready check*, with an optional batch name. Every step shows
live progress; **Pause** and **Stop** work as for any job. A stopped run keeps
everything done and the next run carries on with the same batch.

Without `--batch`, a run takes the batch in progress, else the oldest waiting
one (undated batches last). One batch per run.

## What a run does

1. **Find** - every photo in those categories that is new or changed: date
   taken from EXIF, else from the Google Takeout `.json` beside it.
2. **Batches** - one per calendar month by date taken (`2015-06`), split above
   `batch_size` (`2015-06-1`, `2015-06-2`, ...); photos with no date anywhere in
   batches of `batch_size` (`undated-001`, ...). Photos arriving later for a month
   get a new batch for that month.
3. **Stage 1** - the batch moves to
   `<filestack>/imagepipe/processing/<batch>/<category>/<folders>/`, keeping its
   folders, with the files that belong to each photo (its Takeout `.json`, `.xmp`,
   `.aae`). Each photo's original path is recorded.
4. **Stage 2, ID check and fingerprint**
    - ID: the `<ID>_` at the front of the name, else `XMP-dc:Identifier`. An ID
      the manifest doesn't know is kept and added. An ID that already belongs to
      another photo is a **re-entry**: identical to that photo (as first seen or
      as ImagePipe last wrote it) -> `excluded/01-exact-dup` ("re-entry"); changed
      -> held for the similar-duplicates check (stage 6), flagged
      `reentry-changed`. No ID: a new one.
    - SHA-256 of the file and of its decoded pixels. Decoding the whole image is
      the **decode check**: a photo that doesn't decode goes to
      `excluded/10-corrupt`. (RAW files aren't decoded here.)
    - A wrong extension is corrected (a PNG called `.jpg` becomes `.png`).
    - Flags: **orientation** (a rotation tag that isn't a valid value) and
      **low-res** (long edge under `low_res_pixels`).
5. **Stage 4, exact duplicates** - against this batch and every photo before it.
    - Byte-identical: one copy is kept (any one - they are the same), the rest go
      to `excluded/01-exact-dup/<ID>_<name>`. Every copy's original path is
      recorded against the kept one.
    - Same pixels, different metadata: the kept copy is chosen by pixel count,
      format, EXIF completeness, size, then named faces. What doesn't conflict is
      merged into it now - captions joined (`a | b`), keywords and face regions
      merged, highest rating; camera, title and orientation stay the kept copy's;
      a date or location only one copy has is filled in. Dates and locations that
      disagree go to the Conflicts tab. The other copies go to
      `excluded/02-pixel-dup`.
6. **Stage 2, ID written** - into each kept photo: `XMP-dc:Identifier`, plus the
   Takeout date (as UTC, `+00:00`) and GPS where the photo has none. A Takeout
   value that disagrees with the photo is a conflict instead. The write is safe:
   a copy is written, it must open and read the ID back, and only then replaces
   the original (keeping its modified time). **RAW files are never written**:
   they get a sidecar `<name>.<ext>.xmp` (digiKam's name for it). The photo is
   renamed `<ID>_<name>`.
7. **Burst check** - photos from one camera taken `burst_gap_seconds` or less
   apart, at least `burst_min` in a row, are flagged `burst`. Nothing is removed.

Photos then wait in `processing/<batch>/` for stage 5. A photo with a conflict
waits as `pending-conflict` until you resolve it in the Conflicts tab (see
[Web UI](../web-ui.md#conflicts)); the run carries on with the others.

Every excluded file is renamed `<ID>_<name>`, kept flat in its folder, and gets
a row in `<reports_dir>/excluded.csv`. Each run writes
`<reports_dir>/phase2/phase2-<batch>-<time>.md`.

## The extra checks, and what waits for later stages

| Check | Done now | Completed later |
|---|---|---|
| Full decode | Yes - failures go to `10-corrupt` | - |
| Orientation | Invalid rotation tags flagged | Whether the photo *looks* the right way up: A-Eye, stage 9 |
| Burst | Sequences flagged | Keeping the best one by A-Eye's quality score: stage 9 |
| Low resolution | Small photos flagged | "No bigger copy": the similar-duplicates check, stage 6 (`09-small-no-match`) |
| Ready check | Runs now on any batch | Run automatically before the library (stage 12) |

The **ready check** lists, per photo: an ID in the manifest, the name and the
metadata (or RAW sidecar); a date or marked undated; no conflicts waiting and
not held; no stray sidecars beside it (a Takeout `.json` or `.aae`, or an `.xmp`
that isn't its RAW sidecar - Takeout `.json` files stay beside their photo until
stage 9 merges sidecars); and a format PhotoPrism reads.

## Google Takeout

The `.json` beside a photo is found by its name (`IMG_1.jpg.json`,
`IMG_1.jpg.supplemental-metadata.json`, Takeout's shortened names and `(1)`
variants). Its `photoTakenTime` (UTC) and `geoDataExif`/`geoData` are recorded in
the manifest and written into the photo where the photo has none. A
disagreement is a conflict.
