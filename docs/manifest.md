# Manifest database

One SQLite file (path set by `manifest` in the config) records every file
ImagePipe has ever seen. It runs in WAL mode so reports can be read while a run
is writing.

Keep it on storage that is snapshotted or backed up: it is the pipeline's memory.
It is safe to copy while ImagePipe is stopped.

## Tables

### `files`

One row per file per source.

| Column | Written by | Meaning |
|---|---|---|
| `source`, `rel_path` | catalogue | Which source, and the path relative to it (unique together) |
| `top_folder` | catalogue | First folder under the source, or `(root)` |
| `size`, `mtime` | catalogue | Used to tell whether a file changed since last time |
| `ext` | catalogue | Lower-case extension from the name |
| `kind`, `mime` | catalogue | Real type from the contents, see [Catalogue](stages/catalogue.md#kinds) |
| `ext_mismatch` | catalogue | 1 if the extension doesn't match the contents |
| `sha256` | catalogue | Whole-file fingerprint |
| `width`, `height` | catalogue | Image dimensions |
| `has_exif`, `camera_make`, `camera_model`, `date_taken` | catalogue | Basic EXIF |
| `catalogue_error` | catalogue | Why a file couldn't be read, if it couldn't |
| `pipeline_id` | stage 2 | The file's permanent ID |
| `status` | all | `catalogued`, `missing` (gone from the source; never removed from the manifest), and pipeline states |
| `stage`, `current_path` | pipeline | Where the file is now |
| `excluded_reason`, `keeper_pipeline_id` | pipeline | Why it was excluded and which copy won |
| `first_seen_run`, `last_seen_run` | catalogue | Run IDs |
| `catalogued_at`, `updated_at` | all | Timestamps in the configured timezone |
| `category`, `batch` | phase 2 | `sorted/<category>` it came from; its batch (`YYYY-MM[-n]`, `undated-NNN`) |
| `pixel_hash` | stage 2 | SHA-256 of the decoded pixels (not for RAW) |
| `sha256_written` | stage 2 | SHA-256 after ImagePipe last wrote into the file (`sha256` stays the original's) |
| `date_source`, `undated` | phase 2 | `exif`, `takeout`, `duplicate` or `review`; 1 = no date anywhere |
| `gps_lat`, `gps_lon`, `orientation` | phase 2 | Location and orientation tag |
| `takeout_json`, `takeout` | phase 2 | The Google Takeout `.json` beside the photo and what it said |
| `id_source` | stage 2 | Where the ID came from: `name`, `metadata` or `new` |

Phase 2 statuses: `waiting` (found, batched), `processing` (in
`processing/<batch>`), `processed`, `pending-conflict`, `held` (a re-entry whose
file changed, waiting for stage 6), `excluded`, `error`.

### `batches`

One row per batch: name, month, status (`planned`, `processing`,
`processed`), number of photos.

### `copies`

Every place a photo came from: `original` for the kept file, `duplicate` for
each copy excluded in its favour (with that copy's file id).

### `conflicts`

One row per disagreement waiting for review or resolved: field (`date`, `gps`),
signature (what makes conflicts "similar"), the values with their sources, the
recommendation, and the value chosen.

### `flags`

Per-file notes from the checks: `low-res`, `burst`, `orientation`,
`reentry-changed`.

### `runs`

One row per command run: mode, source, start and finish time, status
(`running`, `finished`, `interrupted`, `failed`) and a JSON summary.

### `events`

Append-only audit log: file changed, file missing, read errors, and (later)
every pipeline decision.

### `archive_members`

When `list_archive_contents` is on: the files inside each `.zip`, with size and
a type guessed from the name.

### `meta`

Holds `schema_version` (2 from 0.5.0). Opening a version-1 manifest adds the
phase 2 tables and columns; nothing is changed or removed. An older ImagePipe
refuses to open a manifest from a newer version.
