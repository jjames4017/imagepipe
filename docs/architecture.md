# Architecture

## The funnel

Files enter at the inbox and move through twelve stages. Each stage can take a
file out of the flow into an [excluded folder](excluded-reasons.md). Whatever
reaches the library is registered in the manifest, and the next drop is checked
against all of it.

```mermaid
flowchart LR
  S[(Sources<br/>read-only)] -->|feeder| I[1 Inbox]
  I --> F[2 ID check +<br/>fingerprint]
  F --> P[3 PDFs]
  P -->|crops re-enter| F
  P --> E[4 Exact dupes]
  E --> C[5 Classify]
  C --> D[6 Similar dupes]
  D --> T[7 Fix dates]
  T --> L[8 Locate]
  L --> A[9 A-Eye]
  A --> K[10 digiKam faces<br/>waits for you]
  K --> Z[11 Snapshot]
  Z --> LIB[12 Library<br/>PhotoPrism + Immich]
  LIB -.registered in.-> M[(Manifest)]
  M -.every check compares against.-> F
```

| # | Stage | What it does |
|---|---|---|
| - | **Catalogue** | Read-only inventory of a source before anything is fed in |
| 1 | Inbox | Files arrive here from the feeder or a manual drop |
| 2 | ID check + fingerprint | Reads an existing pipeline ID from the metadata or the `<ID>_` file-name prefix (re-entry check), otherwise assigns one; SHA-256, pixel hash, perceptual hashes for all 8 rotations/mirrors |
| 3 | PDFs | Identical PDFs out, document PDFs out, photo pages split by ScanSplitter; crops re-enter at stage 2 |
| 4 | Exact dupes | Byte-identical and pixel-identical matches against every file before |
| 5 | Classify | Photo, document, screenshot or meme (SigLIP/CLIP, OCR text density, vision-model tiebreak) |
| 6 | Similar dupes | 97%+ similar, including crops and rotations; keeps the best copy and merges metadata into it; RAW files get a working copy |
| 7 | Fix dates | EXIF repair, scanner detection, approximate dates for scans |
| 8 | Locate | Only for photos with no GPS: time-neighbour GPS, then GeoCLIP / vision guess, tagged with confidence |
| 9 | A-Eye | Rename, blur/accidental-shot check, description - embedded into the file |
| 10 | digiKam faces | Pauses until you have named faces in digiKam and released the batch |
| 11 | Snapshot | ZFS snapshot; files frozen before indexing |
| 12 | Library | PhotoPrism index, Immich read-only external library |

## Folder layout

The working folders sit in **one dataset** so every move between stages is an
instant rename, not a copy:

```text
photostage/               one ZFS dataset (plain folders, not child datasets)
├── inbox/                stage 1
├── work/                 stages 2-11, one subfolder per stage
├── excluded-hold/        exclusions wait here...
└── library/              ...the finished library PhotoPrism and Immich index
```

`excluded-hold/` is drained continuously to the long-term `/excluded/` folder on
bulk storage, with copy → verify checksum → remove, so no file can be lost in
transit.

## Components

ImagePipe is one Python application in one container. It talks to the services
you already run through their APIs rather than bundling them:

| Service | Used by | How |
|---|---|---|
| ScanSplitter | stage 3 | HTTP API |
| Vision model server (vLLM, Ollama or other OpenAI-compatible) | stages 5, 8 | OpenAI-compatible API |
| A-Eye | stage 9 | its API |
| digiKam | stage 10 | you work in its UI; ImagePipe waits for release |
| PhotoPrism, Immich | stage 12 | their APIs (index/rescan, albums) |
| Apprise API | all | HTTP, see [Notifications](notifications.md) |

Every stage can be switched off in the config, so ImagePipe works for someone
who doesn't run A-Eye or digiKam.

## State

The [manifest](manifest.md) records the stage each **file** is at, not each
batch. Each stage works through whatever is waiting for it, so a new drop keeps
flowing while older files wait at the digiKam pause.
