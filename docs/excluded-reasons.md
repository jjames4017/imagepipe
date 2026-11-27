# Excluded reasons

Every photo ImagePipe takes out of the flow goes into one of these folders
under `<filestack>/sorted/excluded/`. Folders are flat; files are renamed
`<pipelineID>_<original name>`. The CSV log records the original path and the
reason.

| Folder | Set by | Meaning |
|---|---|---|
| `01-exact-dup` | stage 2/4 | Byte-identical to a file already seen (including re-entries of a library photo) |
| `02-pixel-dup` | stage 4 | Same pixels, different metadata (metadata merged into the keeper first) |
| `06-screenshot` | stage 5 | Screenshot |
| `07-meme-graphic` | stage 5 | Meme, graphic or other non-photo image |
| `08-size-dup` | stage 6 | Same photo as a better copy: smaller, cropped, rotated or re-compressed |
| `09-small-no-match` | stage 6 | Small image with no larger match yet; checked again on every drop |
| `10-corrupt` | stage 2 | Unreadable or truncated |
| `12-blurry-accidental` | stage 9 | A-Eye reject: blurry, badly exposed or accidental |
| `14-sidecars` | stage 9 | A sidecar whose contents were merged into its image |
| `pdf-source` | triage | The original PDF, after ScanSplitter split its photo pages out (#58) |

Not excluded (decided 27 Nov 2026):

| Was | Goes to |
|---|---|
| `03-pdf-dup`, `04-pdf-source` | PDFs are handled by triage now (#58) |
| `05-document` | `<filestack>/sorted/documents/`, for review before Paperless |
| `11-unsupported` | `<filestack>/sorted/quarantine/unsupported/` |
| `13-uncertain` | `<filestack>/sorted/quarantine/uncertain/` |
| `15-raw` | `<filestack>/sorted/raw-processed/` (a category no pipeline picks up) |

## The CSV log

One file, appended one row per excluded file:

| Column | Meaning |
|---|---|
| `pipeline_id` | The excluded file's ID |
| `original_path` | Where it came from |
| `excluded_path` | Where it is now |
| `reason` | The folder name, plus detail (e.g. "re-entry") |
| `keeper_pipeline_id` | For duplicates: the ID of the copy that was kept |
| `similarity` | For similar duplicates: the similarity % |
| `width`, `height`, `size` | Of the excluded file |
| `timestamp` | When it was excluded |
