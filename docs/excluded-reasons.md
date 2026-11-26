# Excluded reasons

Every file taken out of the flow goes into one of these folders under
`/excluded/`. Folders are flat; files are renamed `<pipelineID>_<original name>`.
The CSV log records the original path and the reason.

| Folder | Set by | Meaning |
|---|---|---|
| `01-exact-dup` | stage 2/4 | Byte-identical to a file already seen (including re-entries of a library photo) |
| `02-pixel-dup` | stage 4 | Same pixels, different metadata |
| `03-pdf-dup` | stage 3 | Identical PDF already seen |
| `04-pdf-source` | stage 3 | The original PDF, kept after its photos were split out |
| `05-document` | stage 3/5 | Not a photo; waits here for review before Paperless |
| `06-screenshot` | stage 5 | Screenshot |
| `07-meme-graphic` | stage 5 | Meme, graphic or other non-photo image |
| `08-size-dup` | stage 6 | Same photo as a better copy: smaller, cropped, rotated or re-compressed |
| `09-small-no-match` | stage 6 | Small image with no larger match yet; checked again on every drop |
| `10-corrupt` | stage 2 | Unreadable or truncated |
| `11-unsupported` | stage 2 | Not an image or PDF |
| `12-blurry-accidental` | stage 9 | A-Eye reject: blurry, badly exposed or accidental |
| `13-uncertain` | any | A low-confidence call, kept for checking |
| `14-sidecars` | stage 9 | A sidecar whose contents were merged into its image |
| `15-raw` | stage 6 | A RAW original, after its working copy was made and checked |

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
