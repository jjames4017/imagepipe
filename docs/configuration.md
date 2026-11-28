# Configuration

ImagePipe reads one YAML file. By default it looks for `/config/imagepipe.yml`;
override with `-c <file>` or the `IMAGEPIPE_CONFIG` environment variable.

All paths are paths **inside the container**. Map host folders to them in your
compose file. Unknown keys are rejected, so a typo is reported rather than
silently ignored.

## Top level

| Key | Required | Default | Meaning |
|---|---|---|---|
| `timezone` | no | `Australia/Brisbane` | Used for timestamps in the manifest, logs and reports |
| `manifest` | yes | | Path of the manifest database file |
| `log_dir` | yes | | Folder for daily log files |
| `reports_dir` | yes | | Folder for reports and CSV exports |
| `sources` | yes | | List of folders ImagePipe reads from |
| `catalogue` | no | see below | Catalogue settings |
| `notify` | no | see below | Notification settings |
| `triage` | no | | Triage settings - see [Triage](stages/triage.md#configuration) |

## `sources`

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Short unique name, used with `--source` |
| `path` | yes | Where ImagePipe sees the folder (container path). Mount it read-only. |
| `host_path` | no | How the folder appears on the host. Used in reports and CSVs so paths make sense outside the container. |
| `exclude` | no | Glob patterns. A file or folder is skipped if its path relative to the source, or its name, matches. Example: `["@eaDir", "#recycle", "*/.Trash-*"]` |

## `catalogue`

| Key | Default | Meaning |
|---|---|---|
| `workers` | `8` | Parallel reader processes. Disk speed is usually the limit; more than the disks can feed doesn't help. |
| `hash` | `sha256` | `sha256` reads every byte to fingerprint each file (needed for duplicate finding). `none` skips it for a fast first look. |
| `read_image_metadata` | `true` | Read dimensions, EXIF, camera and date |
| `use_exiftool` | `true` | Use ExifTool for RAW and video details. Included in the Docker image. |
| `list_archive_contents` | `true` | List the files inside `.zip` archives (names, sizes, a type guessed from the name). Reads only the zip's index; nothing is extracted. RAR, 7z and gzip files are recorded as single files. |
| `batch_size` | `500` | Files per database commit |

## `notify`

| Key | Default | Meaning |
|---|---|---|
| `apprise_url` | `""` | Base URL of your Apprise API server |
| `apprise_key` | `""` | The Apprise config key that routes to your services |
| `tags` | `""` | Optional Apprise tag(s) |
| `events` | `[finished, error]` | Events that send a notification |

Notifications are off unless both `apprise_url` and `apprise_key` are set. See
[Notifications](notifications.md).

## `photoprism_export`

Optional. Uses the PhotoPrism address and login in `tools.photoprism` and the
triage section's filestack. See [PhotoPrism export](stages/ppexport.md).

| Key | Default | Meaning |
|---|---|---|
| `originals` | `/mnt/appdata/imagelibrary` | PhotoPrism's originals folder as the container sees it (mount it read-only) |
| `exclude` | `[imagepipe, library, .ppignore, "*.yml", "*.xmp"]` | Top-level folders or file-name patterns never copied - photo files and other files alike (the `.xmp` sidecars in the originals came from an earlier AI tagging run) |
| `faces` | `all` | `all`: every named face; `manual`: only faces you named |
| `favourite_tag` | `Favourite` | Tag for favourites (they also get rating 5) |
| `review_tag` | `Review` | Tag for photos archived in PhotoPrism |
| `private_tag` | `Private` | Tag for private photos |
| `album_prefix` | `Albums` | Albums are written as `<this>\|<album>` and `<this>/<album>` |

## `phase2`

Optional. Phase 2 also needs the `triage` section (it uses the filestack and the
categories whose pipeline is `imagepipe`).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Phase 2 jobs refuse to run while this is false. Stays off until the PhotoPrism export exists |
| `batch_size` | `2000` | A month with more photos than this is split; undated photos go in batches this big |
| `burst_gap_seconds` | `2` | Photos from one camera this close together count as one burst... |
| `burst_min` | `3` | ...when there are at least this many (flagged only) |
| `low_res_pixels` | `1000` | A photo whose long edge is shorter is flagged low resolution |

The PDF check's setting is in the `triage` section: `pdf_text_words` (default
7) - see [Check PDFs](stages/triage.md#check-pdfs). (`pdf_photo_coverage` and
`pdf_text_chars` from 0.5.0-0.5.1 are still accepted but no longer used.)
