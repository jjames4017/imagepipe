# Rules and decisions

Every design decision, with the reason for it. When the code and this page
disagree, this page describes the intent.

## Never delete

- No stage ever deletes a photo or PDF. A file taken out of the flow is moved to
  `/excluded/<reason>/` - see [Excluded reasons](excluded-reasons.md).
- Excluded folders are **flat**: the original folder structure is not recreated.
  Each file is renamed `<pipelineID>_<original name>` so names never clash and
  each file points straight at its record.
- One CSV log, appended one row per file as it is excluded (files arrive over
  many drops, not all at once). Each row: pipeline ID, original path, excluded
  path, reason, matched keeper's ID, similarity %, dimensions and size.
- Moves between datasets are copy → verify checksum → remove.
- Sources are only ever read. Catalogue and the feeder never change them.

## Pipeline ID

- Assigned at stage 2 the first time a file is seen, written into the file's
  own metadata and recorded in the manifest. It survives A-Eye's renaming.
- The manifest also keeps the original hashes, so a file can be traced after its
  name or metadata change.
- **Re-entry check:** stage 2 first looks for an existing ID.
  - ID found and the file is identical → `01-exact-dup`, reason "re-entry".
  - ID found but the image has changed → through the similar-dupes check against
    that photo; normally `08-size-dup`, or the swap rule if it is better.
  - An ID the manifest doesn't know (e.g. after restoring an older manifest) is
    **kept** and added to the manifest.
- Copies that lost their metadata (messaging apps, social media) have no ID;
  the hash checks in stages 4 and 6 still catch them.

## Duplicates

- Automatic - no review step during the run. The folders and CSV are there to
  review later.
- Exact: byte-identical (SHA-256) and pixel-identical (same picture, different
  metadata).
- Similar: 97% or more. **Cropped and rotated copies count as duplicates.**
  Perceptual hashes are taken for all 8 rotations/mirrors; crops are confirmed
  by feature matching.
- Keeper scoring, in order: pixel count → format → EXIF completeness → file
  size. File size alone is never trusted (a re-saved JPEG can be bigger and
  worse). When copies are equal, the one carrying human work (named faces,
  albums) wins.
- Before a loser is moved, useful metadata it has (dates, GPS) is copied into
  the keeper.
- Burst sequences, Live Photo pairs and RAW+JPEG pairs are never split as
  duplicates of each other.
- Small images with no larger match go to `09-small-no-match` and are checked
  again on every drop. When a larger match arrives, the small file moves to
  `08-size-dup`.

## A better copy arrives later

The better copy replaces the library photo:

1. Every tag is copied from the old copy to the new one: face regions,
   keywords, captions, GPS, dates and the pipeline ID.
2. The new file takes the old file's exact path and name; the old file goes to
   `08-size-dup`.
3. Only that path is rescanned in PhotoPrism and Immich, so they see an updated
   file, not a new photo.
4. Swaps are queued and applied together after a snapshot.

## RAW files

- A RAW file is never the library copy. Its working copy is chosen in this order:
  a full-resolution camera JPEG from a RAW+JPEG pair → the full-resolution JPEG
  preview embedded in the RAW → a darktable-cli render with the default camera
  profile.
- Working copy format: **full-resolution JPEG, quality 95, sRGB**, with all
  camera EXIF copied across. It gets its own pipeline ID, linked in the manifest
  to the RAW it came from.
- The RAW then goes to `15-raw`.
- RAW files are the one exception to embedding: anything written for them uses
  sidecars, because writing into RAW files risks damaging them.

## Metadata in the file

- Tags, names, captions and locations are embedded in the image file rather than
  sidecars, wherever the format allows.
- Where a tool (A-Eye) only produces a sidecar, its contents are merged into the
  file and the sidecar goes to `14-sidecars`.

## Classification

- Screenshots and memes get their own folders (`06`, `07`); they are not deleted.
- Documents go to `05-document` and wait for review before anything goes to
  Paperless.
- Signals: whether a PDF has a text layer and what made it, whether ScanSplitter
  finds photo regions, OCR text coverage, a SigLIP/CLIP classifier, and a vision
  model as tiebreaker. Low-confidence calls go to `13-uncertain`.
- Album pages with handwritten captions stay photos; ID cards and passports are
  documents.

## Location

- **Existing GPS is never overwritten.**
- Only photos with no location get a guess: GPS borrowed from photos taken close
  in time by the same camera, then GeoCLIP, then a vision model.
- Guesses are written with keywords `location/possible` and
  `location/confidence-<n>` (n = confidence %), and listed in a location report.

## Faces

- digiKam is the face master. It suggests names for new photos once you have
  named a few; suggestions stay "unconfirmed" until you accept them.
- The pipeline pauses at stage 10 until you release the batch.
- Names are written into the file as face regions plus `People/<Name>` keywords.

## Archives and Google Takeout

- Catalogue lists what is inside every zip (index only, nothing extracted), so
  zips that hold photos can be found.
- The feeder extracts image files from zips into the inbox. The zip itself stays
  untouched in the source; the manifest records each photo's source as
  `<zip path> → <file inside>`.
- Google Takeout often keeps a photo's real date and GPS only in the `.json`
  file beside it. ImagePipe reads those files and merges the date and location
  into the photo at stages 7 and 8.
- Database dumps (for example Immich `.sql.gz` backups) are catalogued like any
  other file and never enter the photo flow.

## Video

- Live Photo clips stay with their photo.
- A full video pipeline may come later.

## Storage and compute

- Working folders live in one dataset so moves are renames (see
  [Architecture](architecture.md)).
- The manifest lives on the same fast, snapshotted storage as the working
  folders.
- During a major run the GPU is dedicated to ImagePipe.
- Models: SigLIP/CLIP for classification, GeoCLIP for location, a 7-8B vision
  model served by vLLM (OpenAI-compatible, so Ollama or another server can be
  swapped in). FreeToken was reviewed: it does not advertise vision support and
  is built for interactive chat, so it is not used for the bulk run; it could be
  tested later for the `13-uncertain` pile.

## Notifications

All notifications go to an Apprise API server, which forwards them to ntfy and
any other services. See [Notifications](notifications.md).

## Re-processing an existing library

Everything goes back through the full pipeline:

1. **Save existing work first.** Back up PhotoPrism's database, export albums,
   named faces, favourites and edited captions, and write them into the files
   (face regions, `People/<Name>`, `Album/<Name>`, `favourite`). The information
   then travels with the file, and digiKam starts out knowing those people.
2. **Catalogue** each source before feeding it.
3. **Feed in order**: the current library first (it carries human work), then
   older quarantine sets, then raw originals once catalogued.
4. **Throttle** the feeder by free space, since the working storage is smaller
   than the sources.
5. **Switch over** when verified: PhotoPrism gets a new database, albums and
   favourites are rebuilt from keywords through its API; face names in
   PhotoPrism are best-effort.
