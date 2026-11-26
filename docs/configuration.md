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
