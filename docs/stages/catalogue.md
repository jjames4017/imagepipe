# Catalogue

A **read-only inventory** of a source folder. Run it before feeding a source
into the pipeline, to find out what is there and decide what to do with each
kind of file.

```bash
imagepipe catalogue --source <name>
```

## What it does

Three phases, each with live progress on screen:

| Phase | What happens | Progress shows |
|---|---|---|
| 1/3 Scan | Walks the whole tree and records every regular file's path, size and modified time | Files found, folders, data found, rate, elapsed (no ETA: the total isn't known yet) |
| 2/3 Compare | Works out which files are new or changed since the last catalogue, marks unchanged files as seen, marks files no longer present as `missing` | Step counter and what it's doing |
| 3/3 Read | For each new or changed file: real type from contents, SHA-256, dimensions, EXIF, camera, date | Count, %, files/s, data read and MB/s, elapsed, ETA (based on bytes) |

Progress redraws every second on a terminal, or prints a line every 15 seconds
when output goes to a log. A background timer drives it, so it keeps updating
even while one huge file is being hashed.

## Guarantees

- **Never writes to the source.** Files are opened read-only; mount sources `:ro`.
- **Resumable.** Results are saved every `batch_size` files. Ctrl+C keeps
  everything read so far; the next run reads only what's left.
- **Incremental.** A second run only reads new or changed files (by size and
  modified time).
- **Safe if a mount drops.** If the source looks empty but the manifest knows
  files from it, the run stops without marking anything missing.
- **Missing is not deleted.** Files that disappear are marked `missing`; if they
  come back they are restored.

## Kinds

Type comes from the file's contents (magic bytes), not its extension.

| Kind | Examples |
|---|---|
| `image` | JPEG, PNG, GIF, WebP, HEIC, AVIF, TIFF, BMP, JPEG XL, PSD |
| `raw` | CR2, CR3, NEF, ARW, DNG, ORF, RW2, RAF and other camera RAW |
| `video` | MOV, MP4, AVI, MKV, MPEG, MTS, WMV, FLV |
| `audio` | MP3, M4A, WAV, FLAC, OGG |
| `pdf` | PDF |
| `document` | Office files, RTF, text, HTML (including HTML saved with an image name) |
| `archive` | ZIP, RAR, 7z, gzip, bzip2, xz, tar, CAB |
| `sidecar` | XMP, AAE, THM |
| `junk` | `.DS_Store`, `Thumbs.db`, `desktop.ini`, `._*` AppleDouble files |
| `empty` | 0-byte files |
| `unknown` | Anything else |

`ext_mismatch` is set when the extension says one thing and the contents say
another - for example a PNG named `.jpg`, a CR3 named `.jpg`, or a failed
download saved as `photo.jpg` that is really an HTML page.

## Outputs

In `reports_dir/<source>/catalogue-<timestamp>/`:

| File | Contents |
|---|---|
| `summary.md` | Totals, exact-duplicate groups (same SHA-256) and wasted space, mismatches, errors, EXIF coverage, by type, by top-level folder |
| `by_kind.csv` | Files and bytes per kind |
| `by_folder.csv` | Files and bytes per top-level folder per kind |
| `by_extension.csv` | Files and bytes per extension and kind |
| `extension_mismatches.csv` | Every file whose extension doesn't match its contents |
| `errors.csv` | Every file that couldn't be read, with the reason |

Every catalogued file in one CSV:

```bash
imagepipe export-csv --source <name>
```

Regenerate the reports without re-reading anything:

```bash
imagepipe report --source <name>
```

## Dependencies

| Dependency | Version | Used for | Included in the image |
|---|---|---|---|
| Python | 3.11+ | Runtime | yes (3.12) |
| PyYAML | 6+ | Config | yes |
| Pillow | 10+ | Image dimensions and EXIF without decoding pixels | yes |
| pillow-heif | 0.16+ | HEIC/HEIF metadata | yes |
| ExifTool | any recent | RAW and video camera/date details | yes |
| tzdata | any | Timezones in slim containers | yes |
| SQLite | 3.35+ | Manifest (bundled with Python) | yes |

No GPU and no network access are needed.

## Config keys

See [Configuration → catalogue](../configuration.md#catalogue).

## Performance notes

- Phase 3 reads every byte when `hash: sha256`. Expect it to run at roughly the
  speed the source disks can deliver; the ETA is based on bytes, so it is
  accurate for mixed file sizes.
- `hash: none` gives a fast first look (type, size, metadata only) but no
  duplicate information. A later run with `hash: sha256` re-reads the files that
  have no hash yet, so you can start fast and fill the hashes in afterwards.
- The temporary scan tables live in `SQLITE_TMPDIR` (`/data/tmp` in the image),
  not the container's own disk.

## Testing

`tests/test_catalogue.py` builds a tree of generated sample files (no personal
photos) covering every kind, an exact duplicate, a fake HTML `.jpg`, junk files
and archives, then checks: first run, second run reads nothing, changed and new
files are re-read, deleted files become `missing` and come back, an empty source
aborts safely, excludes work, archive listing, reports and CSV export, and that
the source is never modified.
