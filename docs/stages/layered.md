# Layered TIFFs and Photoshop files

Photoshop files (`.psd`, `.psb`) and layered TIFFs are usually design work
(posters, presentations) rather than photos, and Pillow can't read many of them
(16-bit, CMYK, unusual compression). ImagePipe has two commands for them.

## Review list: `imagepipe layered find`

```bash
imagepipe layered find                    # every catalogue source
imagepipe layered find --source vault     # one source (repeatable)
imagepipe layered find --path /some/dir   # any folder (repeatable)
```

Read-only. Lists every PSD/PSB, and every TIFF that is layered (it carries
Photoshop layer data), has several pages, or that Pillow can't read. Plain TIFFs
are not listed. Progress shows for both phases (finding, then inspecting).

The report goes to `<reports_dir>/layered/layered-<time>/`:

| File | What |
|---|---|
| `summary.md` | Counts by type and by reason, how many are identical copies |
| `layered.csv` | One line per file: path, type, why listed, width, height, colour mode, bit depth, layers, whether a merged image is stored, whether Pillow can read it, size, modified time, SHA-256, number of identical copies, notes |

Identical copies sit next to each other in the CSV (sorted by SHA-256).

## Working copies: `imagepipe layered render`

```bash
imagepipe layered render <file> [<file> ...] [--out <folder>]
```

Makes a full-resolution working copy of each file - JPEG, or PNG when it has
transparency; CMYK and 16-bit are converted to normal 8-bit RGB - in
`<reports_dir>/layered/renders` (or `--out`). The original is not touched.

- **PSD/PSB**: the merged image Photoshop stores (when saved with "Maximize
  compatibility", the default), or else a composite of the layers made by
  psd-tools - effects and adjustment layers may not look exactly as in
  Photoshop.
- **TIFF**: the first page (the flattened picture), with Pillow, or tifffile for
  encodings Pillow doesn't read.

This is the same idea as RAW working copies. The pipeline doesn't use it yet:
whether these files go through ImagePipe is decided after reviewing the list.

Needs the `layered` extra (`pip install "imagepipe[layered]"`: psd-tools,
tifffile, imagecodecs); the Docker image includes it.
