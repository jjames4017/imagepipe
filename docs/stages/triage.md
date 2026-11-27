# Triage

Triage sorts large mixed folder trees (photos, documents, archives and
everything else) before anything enters the pipeline. Every file is
identified by its contents, given a pipeline ID and a new name, and moved to
one category folder under `<filestack>/sorted/`:

| Folder | What goes there |
|---|---|
| `sorted/images/` | Photos, with their sidecars (XMP, AAE, Google Takeout JSON) and Live Photo clips. Picked up by ImagePipe. Exact duplicates all go - ImagePipe dedupes them |
| `sorted/raw/` | RAW files, with their sidecars. Picked up by ImagePipe |
| `sorted/documents/` | Documents and PDFs, waiting for review before Paperless. Duplicates all go |
| `sorted/<type>/` | `video`, `audio`, `software`, `disk-images`, `email`, `ebooks`, `code`, `fonts`, `other-known`, `web-pages`, `specials`. Duplicates all go |
| `sorted/quarantine/<reason>/` | `unknown-type`, `corrupt`, `encrypted`, `archive-failed`, `zero-byte`, `system-junk` |

The categories and the extensions in each can be changed (see
[Categories](#categories)). Each category also says which pipeline picks it up,
or none: then the folder is the files' final place.

### The filestack

Everything triage and ImagePipe work on lives under one folder, the
**filestack**, set by `IMAGEPIPE_FILESTACK` in the compose file (it is shown
read-only in the web UI and can only be changed there):

```
<filestack>/
├── triage/work/          archives are extracted here
├── imagepipe/            ImagePipe's working folders (processing, a-eye, digikam, ready)
└── sorted/
    ├── images/ raw/ documents/ video/ audio/ ... specials/ raw-processed/
    ├── quarantine/<reason>/
    └── excluded/<reason>/   photos ImagePipe takes out of the flow
```

**Same-mount rule.** The triage sources and the filestack must be inside the
**same container mount**. Linux refuses hard links and renames between two
mounts, even when both are on the same ZFS dataset or disk, so with separate
mounts every move silently becomes a copy, a checksum and a delete. Mount the
parent folder once (e.g. `/wolf-pool/vault` at the same path) and point
`IMAGEPIPE_FILESTACK` at a folder inside it.

Archives are extracted, every file inside is checked, the contents are sorted
like any other file, and only then is the archive deleted.

Under each destination the **original folder structure is kept**, below a
folder named after the source:

```
/wolf-pool/vault/photos/2014/Bali/IMG_1234.jpg
  -> /wolf-pool/vault/filestack/sorted/images/photos/2014/Bali/IP1M8B3GMSNJ49HBXT_2014-07-12_Denpasar-ID-Bali.jpg
/wolf-pool/vault/file cabinet/Holidays/Fiji 2010.zip  (contains day1/photo1.jpg)
  -> /wolf-pool/vault/filestack/sorted/images/file cabinet/Holidays/Fiji 2010/day1/IP..._2010_Fiji-day1.jpg
```

## Commands

| Command | What it does | Changes files? |
|---|---|---|
| `imagepipe triage scan [--source NAME] [--reread-problems \| --reread-ext TYPES \| --reread-all]` | Inventory: real type, SHA-256, dates, GPS, titles | No |
| `imagepipe triage plan` | Decides every destination and name; writes a dry-run report | No |
| `imagepipe triage apply [--limit N]` | Carries out the plan | **Yes - moves files** |
| `imagepipe triage status` | Counts by status and bucket, recent runs | No |
| `imagepipe triage export-csv` | Every file in the triage database, one CSV | No |
| `imagepipe triage check-pdfs [--limit N]` | Sorts the PDFs in `sorted/documents` - see [Check PDFs](#check-pdfs) | **Yes - moves PDFs, writes photos** |
| `imagepipe triage relocate [--dry-run]` | Moves files sorted before 0.4 from the old folders into the filestack | **Yes - moves files** |

Run them in the foreground (`docker compose run --rm imagepipe triage ...`) so
the live progress shows. Every phase shows a running count, rate and elapsed
time; once the total is known, a percentage and ETA. A scan or apply that is
stopped (Ctrl+C) loses nothing and carries on from where it stopped next time.
To pause instead, press Ctrl+Z and type `fg` to carry on (in the web UI: the
**Pause** button).

`--limit N` stops after N files (or archives): use it for a trial run.

Images with damaged EXIF (Pillow's "Corrupt EXIF data" warning) are not
problems: the image is fine, only some EXIF details such as the date may be
missing. The warning is not printed; the scan summary says how many images had
damaged EXIF.

### Reading files again

A scan only reads files that are new or have changed (size or modified time)
since the last scan; files already read keep their result. After upgrading to
a version that reads some files better, read them again:

- `--reread-problems` - also read again every file that had a read problem
  last time. Quick: only those files are read.
- `--reread-ext TYPES` - also read again every file of these types, e.g.
  `--reread-ext eml` or `--reread-ext eml,msg`. Use it when a new version reads
  one kind of file better.
- `--reread-all` - read every file in the source again, ignoring the unchanged
  check. Takes as long as the first scan.

Only one of these can be used at a time. Only files still in the source (not yet moved
by apply) are read. Files keep their pipeline ID; each one gets a `re-read`
action saying why, and goes back to catalogued, so run `triage plan` again
afterwards. In the web UI: Run tab -> Triage card -> **Scan reads** -> choose (for
**+ re-read file types:** type them in the box beside it, e.g. `eml`), then
**Scan**.

## New names

`<ID>_<date>_<smartname>.<ext>` - the extension is kept, lower-cased. An odd
extension (spaces or symbols, e.g. `.xls whakapapa`) is not used as an
extension; it becomes part of the smartname.

**ID** - see [Pipeline ID](../design.md#pipeline-id). An ID already at the
front of a file name is kept.

**Date**, from the first of:

1. metadata inside the file: EXIF (photos), creation date (video, PDF, Word/Excel/PowerPoint/OpenDocument),
   the `Date:` header of `.eml` emails (in the configured time zone)
2. a sidecar or metadata file: Google Takeout JSON, XMP sidecar
3. a date in the file name, then in the folder names (deepest first) - full date,
   year-month or year; `2014/07/` folders combine to `2014-07`
4. the file's modified time

Two exceptions:

- **Office files**: a date in the file name wins over the Office created date,
  and a descriptive file name is used for the smartname rather than the Office
  title (both are often inherited from a template). The Office title is used
  only when the file name isn't descriptive.
- **Camera clocks that were never set**: an EXIF date of 1 January 1970, 1980
  or 2000-2010, at around midnight or noon (or with no time), is used only if
  no date is found in a sidecar, the file name or the folders. When it is used
  anyway, its source shows as e.g. `exif-default-clock`. A camera model that
  has shown a default date is distrusted for the rest of that year too (its
  clock started at the default and ran on from there) - but its EXIF date is
  only replaced by a sidecar, file-name or folder date more than a year away
  from it, so a correctly set camera of the same model keeps its dates.
- **Ebooks**: a year on its own in an ebook's file name is part of the title
  (`Red Inferno_ 1945`), not its date, and stays in the smartname.

A compact `YYYYMMDDHHMMSS` timestamp in a file name
(`DVR_20121124004613.mp4`) counts as a date and time. A 6-digit `DDMMYY`
number (`Shakespeare 120301`) counts as a date only when its year is within
one year of a year in the folder names, since 6-digit numbers are often
counters; when used it is removed from the smartname. For Office files it also
counts when it is within one year of the Office created date.

URL-encoded names (`Cliff%20Edwards`) are decoded before dates and smartnames
are worked out; the folders on disk are not changed. Generic titles
(`Track 23`, `Chapter 4`, `Untitled`) are never used as smartnames. Month
names joined to other letters count (`WTBDMay2009` -> `2009-05`). UUIDs and
runs of 16+ hex characters are left out of smartnames, and a run of words
repeated straight after itself appears once (`Dansey-Dansey-Census` ->
`Dansey-Census`).

Files going to `quarantine/...` always end with their original file name, so
they can be recognised when reviewing (`..._Harddrive-WTB-Dansey-B8883000`).

When the date comes from inside the file, a sidecar or the file name and a time
is known, the time is added: `2016-05-15_064331` (or `_0643` without seconds).
Dates from folder names or the modified time have no time, and a time of
exactly 00:00:00 counts as none. In a file name only a time straight after the
date counts (`IMG_20190429_164338`, `WP_20160515_06_43_31`); other digit runs
are counters.

**Smartname**: a place name from GPS if there is one (offline lookup, e.g.
`Hoi-An-VN`), then either the file's own title (PDF/Office title, photo
caption, Takeout description) or the meaningful folder names (up to two, e.g.
`Fiji-day1`) plus the file name if it is descriptive. Camera names
(`IMG_1234`, `DSC01234`, `PXL_...`), generic folders (`DCIM`, `100APPLE`,
`Camera Roll`, `New Folder`, `batch_0042` ...) and dates (with any time
straight after them) are left out when they agree with the date the new name
carries, along with joining words left at either end once the dates are gone.
A date that differs is kept, because it says something about the file: a
scanned `Thelma Reynolds 1984.JPG` dated by its 2015 scan becomes
`..._2015-09-16_..._Thelma-Reynolds-1984.jpg`. If nothing is left, the original file name
is used.

Smartnames contain only letters (accented letters and macrons kept, e.g. `ā`),
digits and hyphens: `&` becomes `and`, apostrophes are dropped without
splitting the word (`you're` -> `youre`), and brackets, commas, apostrophes, `@`
and every other symbol are removed (`logo_top (3)` -> `logo-top-3`). A name cut
to `smartname_max_length` is cut at a hyphen, never mid-word. Smartnames never
contain `_` or `.`, so the ID is always everything before the first `_`.

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

Only files with a real archive extension (`.zip`, `.rar`, `.7z`, `.tar`,
`.tgz`, `.tbz2`, `.txz`, `.gz`, `.bz2`, `.xz`, `.cab`) are extracted. Zip or
other archive content under any other extension that triage doesn't know
(`.xlsm`, `.dotx`, `.vsdx`, `.odg`, `.xps`, `.zed`, `.ftmb` ...) goes to
`quarantine/unknown-type` whole, so the original is never unpacked and deleted.

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

## Special file types

- **Password-protected Office files** (`.docx`, `.xlsx`, `.pptx` ...) are not
  zip files inside: they are OLE compound files holding an `EncryptedPackage`
  stream. Triage recognises them, marks them encrypted (rule
  `encrypted-document`) and sends them to `quarantine/encrypted`, untouched.
  An old binary Office file renamed to `.docx`/`.xlsx`, or an RTF/HTML file
  saved under an Office extension, is a normal document and goes to
  `docupipe`. An Office file that looks like a zip but is damaged is still
  reported as a problem.
- **Saved web pages**: a page (`Page.htm`/`.html`) and its `Page_files` folder
  go together to `sorted/web-pages`. The page gets an ID name (rule
  `web-page`); the `Page_files` folder and everything in it keep their
  original names (rule `web-page-files`), so the page's links still work.
  `.mht`/`.mhtml` pages go there too. A `_files` folder without its page counts
  only if it holds web files (`.css`, `.js`, `.htm`), so an ordinary folder
  named e.g. `my_files` is not caught. An `.htm` with no `_files` folder is a
  normal document.
- **Application and genealogy files** (`.ftw`, `.gno`, `.fbk`, `.mpp`, `.vsd`,
  `.pub`, `.tcw`, `.hmk`, `.cl3`, `.obd`, `.paf`, `.ged`) go to
  `sorted/other-known`.
- **Temporary, partial-download and automatic backup files** (`.tmp`,
  `.crdownload`, `.asd`, `.wbk`) go to `quarantine/system-junk`.
- **Photoshop files** (`.psd`, `.psb`): Pillow can't open most of them, so the
  width and height come from the Photoshop header and the date and title from
  exiftool (when installed). They go to `imagepipe` like other images.

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

The bucket (category, or `quarantine/<reason>`) for each file, in order:

| Rule | Bucket |
|---|---|
| empty file | `quarantine/zero-byte` |
| `.DS_Store`, `Thumbs.db`, `photothumb.db`, `desktop.ini`, `._*` and similar | `quarantine/system-junk` |
| can't be read | `quarantine/corrupt` |
| extension moved to another category in `categories` | that category |
| `.tmp`, `.crdownload`, `.asd`, `.wbk`, and Office lock files (`~$...`) | `quarantine/system-junk` |
| the extension promises an image/RAW/video/audio/PDF but the contents are text, HTML or unknown | `quarantine/corrupt` |
| image | `images` |
| RAW | `raw` |
| XMP/AAE/THM sidecar, Takeout JSON | with its photo; `images` if its photo isn't found |
| short video beside a same-named photo (Live Photo) | with its photo |
| PDF | `documents` |
| design / vector files (`.psd .psb .xcf .kra .afdesign .afphoto .indd .svg .svgz .eps .ai .cdr .wmf .emf .ps .dxf .dwg`), `.mpo`, `.ico`, and TIFFs that are multi-page, layered or unreadable | `specials` (reviewed one by one) |
| saved web page and its `_files` folder, `.mht` | `web-pages` |
| Word, Excel, PowerPoint, OpenDocument, Pages/Numbers/Keynote, RTF, text, Markdown, CSV, HTML (by extension) | `documents` |
| document content under an extension triage doesn't know (e.g. `.tibuq6`) | `quarantine/unknown-type` |
| video, audio | `video`, `audio` |
| archive with a real archive extension | extracted (see above) |
| archive content under any other unknown extension | `quarantine/unknown-type` |
| known extension (email, ebooks, software, disk images, fonts, code, other known data files) | that category |
| anything else | `quarantine/unknown-type` |

## Configuration

The `triage:` section of the config file, plus `IMAGEPIPE_FILESTACK` in the
compose file (see [The filestack](#the-filestack)). Paths are as the container
sees them; mount `/wolf-pool/vault` at the same path so they match the host.

| Key | Default | Meaning |
|---|---|---|
| `database` | required | Triage database file |
| `log_dir`, `reports_dir` | required | Logs; reports and `actions.csv` |
| `sources` | required | `name`, `path`, optional `exclude` (glob patterns) |
| `categories` | built in | Category name -> `pipeline` (`none` or `imagepipe`) and `extensions`. See below |
| `workers` | 8 | Parallel readers |
| `verify` | `auto` | `auto`: inode check for same-disk moves, SHA-256 for copies. `hash`: SHA-256 always |
| `embed_metadata` | `[]` (off) | Kinds to write the ID into (`image`, `pdf`, `video`) - see above |
| `reverse_geocode` | `true` | GPS to place name |
| `day_first` | `true` | `03-04-2015` in a name means 3 April |
| `live_photo_max_bytes` | `20MB` | Largest video treated as a Live Photo clip |
| `max_archive_depth` | 5 | Archives inside archives |
| `max_extract_bytes` | `200GB` | Largest unpacked size |
| `smartname_max_length` | 80 | Characters |
The filestack and the sources must not sit inside each other.

### Categories

Without `categories` in the config, triage uses the built-in list: `images`,
`raw`, `documents`, `video`, `audio`, `ebooks`, `email`, `code`, `fonts`,
`software`, `disk-images`, `web-pages`, `other-known`, `specials`,
`raw-processed`. `images` and `raw` go to ImagePipe; the rest to no pipeline.
Edit them in the web UI (Settings -> Triage -> Categories) or in the file:

```yaml
triage:
  categories:
    images: {pipeline: imagepipe, extensions: [jpg, jpeg, png, heic, ...]}
    documents: {pipeline: none, extensions: [pdf, docx, json, ...]}   # json moved here from other-known
```

- An extension can be in only one category. Moving one to another category
  sends files with that extension there; checks for empty, junk, unreadable,
  encrypted and mislabelled files still come first.
- One pipeline per category. `quarantine`, `excluded` and `extract` can't be
  category names.
- Files of content triage recognises without the extension (a photo, a PDF, a
  video) go to `images`, `raw`, `documents`, `video` or `audio` unless their
  extension was moved.

### Moving from the layout before 0.4

Before 0.4 the config had `work_dir` and `destinations` (`imagepipe`,
`docupipe`, `sorted`, `quarantine`). Those keys still load (the web UI keeps
them when it saves) so that `imagepipe triage relocate` knows where files are:

| Old place | New place |
|---|---|
| `<imagepipe>/...` | `<filestack>/sorted/images/...`, RAW files (and their companions) `sorted/raw/...` |
| `<docupipe>/...` | `<filestack>/sorted/documents/...` |
| `<sorted>/<type>/...` | `<filestack>/sorted/<type>/...` |
| `<quarantine>/<reason>/...` | `<filestack>/sorted/quarantine/<reason>/...` |

Folders below are kept exactly. Moves are the same safe move as apply, the
triage database follows every file (with a `relocated` action), and every
move is listed in `reports_dir/relocate-<time>/moves.csv`. Files under the old
folders that triage has no record of are moved by the same rules. Old folders
left empty are removed. Run `--dry-run` first; afterwards run `triage plan`
again, and delete the old keys from the config.

## Dependencies

All are in the Docker image:

- Python 3.11+, Pillow, PyYAML (required)
- exiftool - dates/GPS from RAW, video, audio and PDF (and writing tags, if turned on)
- 7-Zip (`7zz` or `7z`) - `.7z`, `.rar`, `.cab`
- `reverse_geocoder` (`pip install imagepipe[triage]`) - offline GPS to place name
- `pillow-heif` - HEIC photos

Each optional one that is missing is reported once, and triage carries on
without it.

## Check PDFs

`imagepipe triage check-pdfs` (Run tab: **Check PDFs**) is a job of its own,
separate from scan, plan and apply, with its own progress, Pause and Stop. Each
run checks every PDF in `<filestack>/sorted/documents/` it hasn't checked yet
(the first run covers everything already sorted; later runs only new or changed
PDFs), and follows up PDFs waiting for review. `--limit N` (Run tab: the
**limit** box) checks at most N new PDFs, for a trial run; the next run carries
on with the rest. PDFs waiting for review are always followed up.

Each PDF is first read for words, page by page: its text layer, and Tesseract
OCR when a page's text layer has **`pdf_text_words`** words or fewer (default
7). A word is a run of 2 or more letters or digits, so OCR specks on a photo
don't count.

| PDF | Goes to |
|---|---|
| any page has more than `pdf_text_words` words | a document: stays in `sorted/documents/`. Checking stops at that page and the PDF never goes to ScanSplitter |
| no page has, and ScanSplitter finds photos on every page | split by ScanSplitter (below) |
| no page has, and any page has no photos (or detection failed) | `sorted/quarantine/pdf-uncertain/` |
| too big for ScanSplitter: over 200 MB or 200 pages, or a page too large for it to decode | `sorted/quarantine/pdf-uncertain/` (listed as too large) |
| can't be read (the PDF reader fails, or it has no pages) | `sorted/quarantine/pdf-uncertain/` (listed as unreadable) |

**Splitting.** Each run makes its own ScanSplitter project
("ImagePipe PDF check <date> <time>") and uploads the photo PDFs into it.
Pages ScanSplitter raised no CHECK flag for are approved by ImagePipe and their
photos exported straight away to `sorted/images/` (same folders as the PDF,
named `<PDF name>_p<page>_<n>.jpg`). Pages with a CHECK flag wait for you in
ScanSplitter's Projects page: fix the boxes and approve them there (or delete a
page to skip it). Every later run exports the pages you approved. When all
pages of a PDF are done the PDF moves to `sorted/excluded/pdf-source/`.
ScanSplitter's own Deliver is never used. A run's project is deleted again at
the end if nothing in it is waiting for review.

Pages of PDFs that turn out not to be photo PDFs are taken out of the project
again, so the review list only holds real photo pages. Every move and every
exported photo is in `actions.csv` and the Actions tab. `imagepipe triage
status` shows the counts by verdict.

Needs ScanSplitter's address (Settings > External tools); the Docker image
includes Tesseract and Poppler. If ScanSplitter can't be reached the job stops
without changing anything for the PDF it was on.
