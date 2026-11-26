# Triage

Triage sorts large mixed folder trees (photos, documents, archives and
everything else) before anything enters the pipeline. Every file is
identified by its contents, given a pipeline ID and a new name, and moved to
one place:

| Destination | What goes there |
|---|---|
| `imagepipe/` | Photos and RAW files, with their sidecars (XMP, AAE, Google Takeout JSON) and Live Photo clips. Exact duplicates all go - ImagePipe dedupes them |
| `docupipe/` | Documents and PDFs, staged for review before Paperless. Duplicates all go |
| `sorted/<type>/` | `video`, `audio`, `software`, `disk-images`, `email`, `ebooks`, `code`, `fonts`, `other-known`. Duplicates all go |
| `quarantine/<reason>/` | `unknown-type`, `corrupt`, `encrypted`, `archive-failed`, `zero-byte`, `system-junk` |

Archives are extracted, every file inside is checked, the contents are sorted
like any other file, and only then is the archive deleted.

Under each destination the **original folder structure is kept**, below a
folder named after the source:

```
/wolf-pool/vault/photos/2014/Bali/IMG_1234.jpg
  -> /wolf-pool/vault/imagepipe/photos/2014/Bali/IP1M8B3GMSNJ49HBXT_2014-07-12_Denpasar-ID-Bali.jpg
/wolf-pool/vault/file cabinet/Holidays/Fiji 2010.zip  (contains day1/photo1.jpg)
  -> /wolf-pool/vault/imagepipe/file cabinet/Holidays/Fiji 2010/day1/IP..._2010_Fiji-day1.jpg
```

## Commands

| Command | What it does | Changes files? |
|---|---|---|
| `imagepipe triage scan [--source NAME]` | Inventory: real type, SHA-256, dates, GPS, titles | No |
| `imagepipe triage plan` | Decides every destination and name; writes a dry-run report | No |
| `imagepipe triage apply [--limit N]` | Carries out the plan | **Yes - moves files** |
| `imagepipe triage status` | Counts by status and bucket, recent runs | No |
| `imagepipe triage export-csv` | Every file in the triage database, one CSV | No |

Run them in the foreground (`docker compose run --rm imagepipe triage ...`) so
the live progress shows. Every phase shows a running count, rate and elapsed
time; once the total is known, a percentage and ETA. A scan or apply that is
stopped (Ctrl+C) loses nothing and carries on from where it stopped next time.

`--limit N` stops after N files (or archives): use it for a trial run.

## New names

`<ID>_<date>_<smartname>.<ext>` - the extension is kept, lower-cased.

**ID** - see [Pipeline ID](../design.md#pipeline-id). An ID already at the
front of a file name is kept.

**Date**, from the first of:

1. metadata inside the file: EXIF (photos), creation date (video, PDF, Word/Excel/PowerPoint/OpenDocument)
2. a sidecar or metadata file: Google Takeout JSON, XMP sidecar
3. a date in the file name, then in the folder names (deepest first) - full date,
   year-month or year; `2014/07/` folders combine to `2014-07`
4. the file's modified time

**Smartname**: a place name from GPS if there is one (offline lookup, e.g.
`Hoi-An-VN`), then either the file's own title (PDF/Office title, photo
caption, Takeout description) or the meaningful folder names (up to two, e.g.
`Fiji-day1`) plus the file name if it is descriptive. Camera names
(`IMG_1234`, `DSC01234`, `PXL_...`), generic folders (`DCIM`, `100APPLE`,
`Camera Roll`, `New Folder`, `batch_0042` ...) and dates are left out. If
nothing is left, the original file name is used. Smartnames never contain `_`
or `.`, so a name always splits cleanly into its three parts.

Where the date and smartname came from is recorded for every file
(`date_source`, `smartname_source`), so a guessed date is never mistaken for a
real one.

**Companions** (sidecars, Takeout JSON, Live Photo clips) take their photo's
name with their own extension, so they stay paired:
`IP..._2014-07-12_Bali.jpg` + `IP..._2014-07-12_Bali.xmp` + `IP..._2014-07-12_Bali.jpg.json`.

## Nothing is written into the files

Triage does not change file contents. Each file's pipeline ID is at the front
of its new name and in the triage database, with its original location. So
exact duplicates stay byte-identical, and nothing is rewritten twice:

- photos: ImagePipe stage 2 reads the ID from the file name and writes it into
  the file (`XMP-dc:Identifier`) together with its other tags
- documents: reviewed in `docupipe`, then Paperless takes over

If you do want triage to write the ID (`XMP-dc:Identifier`) and original
location (`XMP-dc:Source`) into files, list the kinds in `embed_metadata`
(`image`, `pdf`, `video`). This rewrites every such file and reads it back, and
the file's SHA-256 after tagging is recorded as `sha256_final`. RAW files are
never written to.

## How files are moved safely

- Before each move the file is checked against the scan (size and modified
  time). A file that changed is skipped (`changed`) - scan and plan again.
- Same file system: the file is hard-linked to its new name, checked to be the
  same file, then the old name is removed. Nothing is ever overwritten - a
  taken name gets `-1`, `-2` ...
- Different file systems: copied to `<name>.triage-partial`, flushed to disk,
  its SHA-256 compared with the scan's, renamed into place, then the original
  is removed.
- A move stopped part way is finished off at the start of the next `apply`.
- Empty source folders are left in place.

## Archives

Supported: `.zip`, `.tar`, `.tgz`/`.tar.gz`, `.tar.bz2`, `.tar.xz`, single
`.gz`/`.bz2`/`.xz`, and (with 7-Zip) `.7z`, `.rar`, `.cab`. Zip-based formats
that aren't really archives (`.docx`, `.epub`, `.cbz`, `.apk`, `.jar` ...) are
not extracted.

1. Extracted into `work_dir/<archive ID>/`. Paths are cleaned so nothing can
   land outside that folder; links are refused.
2. Checked: zip CRC-32 and sizes, tar sizes, gz/bz2/xz checksums, 7-Zip's own
   test then sizes.
3. Every extracted file is read back and its SHA-256 compared with the one
   taken while extracting.
4. The files are planned and moved like any other. Their original structure is
   the archive's folder plus a folder named after the archive
   (`Holidays/Fiji 2010.zip` -> `Holidays/Fiji 2010/...`). Their `origin` is
   `<archive path>::<path inside>`.
5. Only then is the archive deleted (action `archive-deleted`, with its SHA-256).

Archives inside archives are handled the same way, up to `max_archive_depth`.
A password-protected archive goes to `quarantine/encrypted`; one that can't be
read, fails a check, is empty, unpacks to more than `max_extract_bytes` or is
nested too deep goes to `quarantine/archive-failed` - untouched.

Before extracting, triage checks there is room in `work_dir`; if not, apply
stops with a message rather than filling the disk.

## Problems with single files

One file never stops a run. If a file can't be listed, read, recorded,
planned or moved - or an archive crashes while being extracted - that file is
left where it is, marked `error` with the reason in the action log, and the run
carries on with everything else. The summary and notification give the count.

Only problems that affect every file stop a run: a source that isn't mounted,
a destination that can't be written, or not enough space to extract an archive.

File names that aren't valid UTF-8 (often from old Windows or Mac machines) are
handled: the file keeps its exact original folder names at its destination, and
its new file name is clean UTF-8 (the odd characters are left out of the
smartname). In the database and reports these characters show as symbols from a
private-use range (U+F780-U+F7FF).

## The record of what happened

- **Triage database** (`database`): one row per file - triage ID, source,
  original path, where it is now, type, SHA-256 before and after tagging,
  dates, GPS, title, bucket, the rule that chose it, the companion it belongs
  to, the archive it came out of. Plus a table of every action.
- **`reports_dir/actions.csv`**: one line per action, appended as it happens -
  `moved`, `extracted`, `archive-deleted`, `rerouted`, `tags-written`,
  `tags-failed`, `skipped`, `missing`, `error` - with from/to paths, rule,
  checksums and detail.
- **`reports_dir/plan-<time>/`**: `plan.csv` (every planned file) and
  `summary.md` (counts per bucket, rule, date source, smartname source, and
  examples).
- **`reports_dir/apply-<time>.md`**: what each apply run did.
- **`log_dir/triage-<date>.log`**: the run log.

## Rules

The bucket for each file, in order:

| Rule | Bucket |
|---|---|
| empty file | `quarantine/zero-byte` |
| `.DS_Store`, `Thumbs.db`, `desktop.ini`, `._*` and similar | `quarantine/system-junk` |
| can't be read | `quarantine/corrupt` |
| extension listed in `bucket_overrides` | as configured |
| the extension promises an image/RAW/video/audio/PDF but the contents are text, HTML or unknown | `quarantine/corrupt` |
| image or RAW | `imagepipe` |
| XMP/AAE/THM sidecar, Takeout JSON | with its photo; `imagepipe` if its photo isn't found |
| short video beside a same-named photo (Live Photo) | with its photo |
| PDF | `docupipe` |
| Word, Excel, PowerPoint, OpenDocument, Pages/Numbers/Keynote, RTF, text, Markdown, CSV, HTML | `docupipe` |
| video, audio | `sorted/video`, `sorted/audio` |
| archive | extracted (see above) |
| known extension (email, ebooks, software, disk images, fonts, code, other known data files) | `sorted/<type>` |
| anything else | `quarantine/unknown-type` |

## Configuration

The `triage:` section of the config file. Paths are as the container sees
them; mount `/wolf-pool/vault` at the same path so they match the host.

| Key | Default | Meaning |
|---|---|---|
| `database` | required | Triage database file |
| `log_dir`, `reports_dir` | required | Logs; reports and `actions.csv` |
| `work_dir` | required | Where archives are extracted. Put it on the same file system as the destinations |
| `sources` | required | `name`, `path`, optional `exclude` (glob patterns) |
| `destinations` | required | `imagepipe`, `docupipe`, `sorted`, `quarantine` |
| `workers` | 8 | Parallel readers |
| `verify` | `auto` | `auto`: inode check for same-disk moves, SHA-256 for copies. `hash`: SHA-256 always |
| `embed_metadata` | `[]` (off) | Kinds to write the ID into (`image`, `pdf`, `video`) - see above |
| `reverse_geocode` | `true` | GPS to place name |
| `day_first` | `true` | `03-04-2015` in a name means 3 April |
| `live_photo_max_bytes` | `20MB` | Largest video treated as a Live Photo clip |
| `max_archive_depth` | 5 | Archives inside archives |
| `max_extract_bytes` | `200GB` | Largest unpacked size |
| `smartname_max_length` | 80 | Characters |
| `bucket_overrides` | `{}` | Extension to bucket, e.g. `{json: docupipe}` |

Destinations, `work_dir` and sources must not sit inside each other.

## Dependencies

All are in the Docker image:

- Python 3.11+, Pillow, PyYAML (required)
- exiftool - dates/GPS from RAW, video, audio and PDF (and writing tags, if turned on)
- 7-Zip (`7zz` or `7z`) - `.7z`, `.rar`, `.cab`
- `reverse_geocoder` (`pip install imagepipe[triage]`) - offline GPS to place name
- `pillow-heif` - HEIC photos

Each optional one that is missing is reported once, and triage carries on
without it.
