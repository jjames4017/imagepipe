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

Holds `schema_version`. Later versions will migrate the schema forward; an
older ImagePipe refuses to open a manifest from a newer version.
