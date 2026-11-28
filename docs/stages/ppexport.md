# PhotoPrism export

Copies the photos PhotoPrism holds into the pipeline, with the work you did in
PhotoPrism written into the copies. PhotoPrism's originals are only read, never
changed, so PhotoPrism keeps working until you switch over.

PhotoPrism's database (read through its API, with the login in Settings >
External tools > PhotoPrism) is the source of truth for what you did there.

## What is written into each copy

| In PhotoPrism | Written as | When |
|---|---|---|
| Title | `XMP-dc:Title` | Only if you set it (source manual/batch/admin); AI titles are ignored |
| Caption | `XMP-dc:Description` | Only if you set it; AI captions are ignored |
| Keywords | keywords (`XMP-dc:Subject`) | Only keywords you added |
| Date and time | `DateTimeOriginal` (+ offset from the photo's time zone) | Only if you changed it; PhotoPrism's estimates are ignored |
| Location | GPS | Only if you changed it; estimates are ignored |
| Favourite | rating 5 and the tag `Favourite` | Always |
| Your albums | the album name as a keyword, `Albums\|<name>` (Lightroom) and `Albums/<name>` (digiKam) | Albums you made only - not folders, moments, months or states |
| Named faces | face areas (MWG regions, as digiKam reads them), merged with any already in the file | Every face with a name (`faces: manual` - only ones you named) |
| Archived | the tag `Review` | Always |
| Private | the tag `Private` | Always |

Values PhotoPrism took from the file itself are already in the file and are left
as they are. Everything PhotoPrism estimated is left for ImagePipe's own checks.

!!! note "Rotated photos"
    PhotoPrism places face areas on the photo as it is shown. For a photo with an
    orientation tag, digiKam may show those areas turned.

## Where files go

- Photos, RAW files and the files that belong to them (Live Photo clips, `.xmp`,
  `.json`, `.aae`) → `<filestack>/imagepipe/intake/photoprism/<original folders>/`.
  Phase 2 finds them first and gives them their own batches (`pp-2015-06`, ...),
  which are all taken before any batch from `sorted/`.
- Anything else in the originals → `<filestack>/triage/intake/photoprism/`, for
  triage (add that folder as a triage source when you want it sorted).
- Files and folders matching `exclude` are not copied at all, even when PhotoPrism lists them as part of a photo: by default PhotoPrism's own `.yml` sidecars and the `.xmp` sidecars an earlier AI tagging run left beside the photos (AI descriptions and keywords, dates without times). They stay in the originals; the report counts them as `left-out`.

Each copy is made next to its destination, checked against PhotoPrism's SHA-1 of
the original, written into, checked again (it must still open), then renamed into
place. RAW files get a `<name>.<ext>.xmp` sidecar instead.

## Commands

| Command | What it does | Changes files? |
|---|---|---|
| `imagepipe photoprism-export --dry-run` | Report of what would be copied and written | No |
| `imagepipe photoprism-export [--limit N]` | Copies and writes (`--limit`: the first N photos, for a trial) | **Copies files** (originals untouched) |

Run tab: **PhotoPrism export** - *Export (dry run)* and *Export - copies files*
(asks first), with an optional limit. Live progress; **Pause** and **Stop** work
as for any job, and a stopped run carries on where it stopped (finished files are
recorded in the manifest). The report is on the Reports tab under
`ppexport-<time>/`: `files.csv`, `files-other.csv` and `summary.md`.

## Setting up

PhotoPrism's originals must be visible inside the ImagePipe container,
**read-only**, at the path in Settings > PhotoPrism export > originals.
