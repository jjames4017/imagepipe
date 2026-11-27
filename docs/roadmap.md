# Roadmap

ImagePipe is built in phases. Each phase is tested on the generated test set,
then on a small real drop, before the next begins.

| Phase | Contents | Status |
|---|---|---|
| 1 | Foundation (config, manifest, logging, progress, Apprise notifications, Docker, CI, docs) and **Catalogue** | Done (0.1) |
| 1b | **Triage**: sort mixed folder trees (photos, documents, archives, everything else) into ImagePipe's inbox, a document staging folder, sorted type folders or quarantine, with renaming, archive extraction and a full action log | Done (0.2) |
| 1c | **Web UI**: settings form and raw YAML editor, run every command with live progress and stop, plan review, action log, reports | Done (0.3) |
| 2 | Feeder (copies from read-only sources, throttled by free space; extracts image files from zip archives, recording `<zip> → <member>` as the source), Google Takeout JSON reader (restores dates and GPS kept only in Takeout's `.json` files), stages 1-4 (inbox, ID check + fingerprint, PDFs via ScanSplitter, exact dupes), excluded folders + CSV log, cross-dataset copy-verify-remove, status web page | In progress (0.5): stages 1, 2 and 4, Takeout dates/GPS, excluded folders + CSV log, conflict review, decode/orientation/burst/low-res flags and the ready check are built; PDFs moved to triage (`check-pdfs`). Not yet: feeder from read-only sources, cross-dataset copy |
| 3 | Stage 5 classify (SigLIP/CLIP, OCR, vision tiebreak), stage 6 similar dupes (rotation/crop aware, keeper scoring, metadata merge), RAW working copies, small-image recheck | Planned |
| 4 | Stage 7 dates, stage 8 location (time neighbours, GeoCLIP, vision guess) | Planned |
| 5 | Stage 9 A-Eye (with sidecar merge), stage 10 digiKam pause and release | Planned |
| 6 | Stages 11-12 (snapshot, PhotoPrism + Immich), better-copy swap, PhotoPrism export and restore | Planned |
| 7 | Re-processing the existing library end to end | Planned |
