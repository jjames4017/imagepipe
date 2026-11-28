# Web UI

`imagepipe serve` runs a small web page for settings, running jobs and reading
reports. It is built into the image - no extra container.

```
http://<host>:8020/
```

There is **no login**: keep it on your LAN.

## Pages

| Page | What it does |
|---|---|
| Run | Buttons for catalogue, catalogue reports/CSV, **Check PDFs** (see [Triage](stages/triage.md#check-pdfs)), **Phase 2** (find photos and make batches; run a batch; ready check - see [Phase 2](stages/phase2.md)), triage scan (with a choice to re-read problem files, chosen file types, or everything), plan, apply (with an optional trial-run limit) and the notification test. Shows the running job's live progress as labelled columns - Phase, Done / total, %, Rate, Data, Elapsed, ETA, and Detail (what it is doing now: the folder being planned, the file being moved, problems found) - and its output, with **Pause**/**Resume** and **Stop** buttons. One job at a time |
| Status | The same as `imagepipe status`, `imagepipe triage status` and `imagepipe phase2 status` |
| Plan review | Every file the last triage plan placed - from, to, bucket, rule, date and smartname and where each came from - filterable by bucket and rule, and searchable. Nothing here has moved yet |
| Files | Search the files triage has placed and move them to another triage destination - see [Moving files by hand](stages/triage.md#moving-files-by-hand-files-tab) |
| Conflicts | Dates/times (time zone included) and locations that disagree, from phase 2 - see [Conflicts](#conflicts). The tab shows how many are waiting |
| Actions | Everything triage has done, newest first, filterable and searchable (the same as `actions.csv`) |
| Reports | Every report and log, to read in the page or download, and the JPEG/PNG working copies from `imagepipe layered render` (click to view the picture). **Delete** next to each removes that file; **Delete all logs** removes every log and report listed. Both ask first and are refused while a job is running. The audit trail (`actions.csv`) and the triage database are always kept, and folders left empty are removed |
| Settings | Every setting in the config file, as a form or as raw YAML. Triage shows the filestack folder read-only (it comes from `IMAGEPIPE_FILESTACK` in the compose file) and the **Categories** list: each category's extensions and the pipeline that picks it up, or none. **External tools**: the address (and login or API key) of ScanSplitter, PhotoPrism, Immich, A-Eye and digiKam, each with a **Test** button |

**Apply asks first.** It is the one job that moves files, so the page shows how
many files are planned and asks you to confirm.

**Stop** is the same as Ctrl+C in a terminal: the job stops cleanly and keeps
everything it has done; running it again carries on.

**Saving settings** checks every value with the same checks the command line
uses, and only saves if they pass. The previous file is kept beside it as
`imagepipe.yml.bak-<date>-<time>`. The form writes a clean file, so comments are
not kept; the Raw YAML view saves exactly what you type. Changes apply to the
next job you run.

## How jobs run

Each job is a separate `imagepipe ...` process, exactly as if you typed the
command - the page only starts it and shows its output. Each finished job's
output is also saved as `ui-job-<time>.log` in the log folder.

If the container is stopped while a job is running, the job is stopped the
same way as the Stop button. Triage apply finishes off any half-done move on its
next run.

## Setting it up

In the compose file:

- `command: ["serve"]` and `restart: unless-stopped`, so the page stays up
- a port, e.g. `8020:8020` (change the left-hand number to use another host port)
- the config folder mounted **read-write**, so Settings can save

`imagepipe serve --port N` changes the port inside the container;
`--host` the address it listens on (default: all).

## Pausing a job

**Pause** freezes the running job where it is - the job and all its worker
processes (and exiftool) stop using CPU and disk - and **Resume** carries on
from exactly the same point. Time spent paused doesn't count in the elapsed
time or the ETA. Nothing is lost, but a paused job still holds its place: no
other job can start until it is resumed or stopped. **Stop** works on a paused
job too (it resumes it just long enough to stop cleanly).

A pause lasts only while the container runs. If the container is restarted
while a job is paused, the job ends as if stopped; scan and apply carry on from
where they were next time, and plan starts over (keeping every ID it gave).

## External tools

Settings -> **External tools** holds where each outside tool is: ScanSplitter,
PhotoPrism (address, username, password), Immich (address, API key), A-Eye
(address, username, password) and digiKam (address, username, password). Nothing is hard-coded, so the same image works on any system.
Apprise stays under **Notifications** and is tested with the Run tab's
"Send a test notification".

**Test** checks the tool from inside the ImagePipe container with what is in the
form (saved or not) and changes nothing in the tool:

| Tool | Test |
|---|---|
| ScanSplitter | `GET /api/health` answers `ok` |
| PhotoPrism | `GET /api/v1/status` answers `operational`; with a username and password, signs in and out again |
| Immich | `GET /api/server/ping` answers `pong`; with an API key, `GET /api/users/me` accepts it |
| A-Eye | `GET /api/health` answers; with a username and password, signs in to `GET /api/watch/status` (HTTP Basic, read-only) |
| digiKam | the web page answers; with a username and password, signs in (HTTP Basic). Without them a page that asks for a login fails the Test |

HTTPS addresses with self-signed certificates are accepted. Passwords and keys
are saved in `imagepipe.yml` in plain text.

A container on Docker's host network can't reach containers on a macvlan
network of the same host (and the reverse) - a Test that times out may mean
that, not a wrong address.

## Conflicts

Phase 2 never writes a date or location that disagrees with another source for
the same photo. Each disagreement becomes a card in the **Conflicts** tab:

- a thumbnail of the kept copy (and of the other copies, for duplicates)
- each value with where it came from: the photo's EXIF, another copy, or the
  Google Takeout `.json`
- a **recommended** value with the reason, where there is one. For a date that
  is the same moment written once in local time and once in UTC, it keeps the
  local time and adds the time zone. For a location it is the value most photos
  from the same camera within an hour agree with, with a confidence %; it is
  never applied by itself (an image-based estimate comes with stage 8)
- **Use this** next to each value, and **apply this choice to all similar
  conflicts too** - similar means the same field, the same kinds of source and,
  for dates, the same difference (for example "camera time vs Takeout time,
  +10:00")

The value is written into the photo with the same safe write as stage 2 (a copy
is written and checked, then swapped in; RAW files get their sidecar). The photo
waits as `pending-conflict` until every conflict for it is resolved; the run
carries on with the other photos meanwhile.
