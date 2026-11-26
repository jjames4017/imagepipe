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
| Run | Buttons for catalogue, catalogue reports/CSV, triage scan (with a choice to re-read problem files or everything), plan, apply (with an optional trial-run limit) and the notification test. Shows the running job's live progress as labelled columns - Phase, Done / total, %, Rate, Data, Elapsed, ETA, and Detail (what it is doing now: the folder being planned, the file being moved, problems found) - and its output, with **Pause**/**Resume** and **Stop** buttons. One job at a time |
| Status | The same as `imagepipe status` and `imagepipe triage status` |
| Plan review | Every file the last triage plan placed - from, to, bucket, rule, date and smartname and where each came from - filterable by bucket and rule, and searchable. Nothing here has moved yet |
| Actions | Everything triage has done, newest first, filterable and searchable (the same as `actions.csv`) |
| Reports | Every report and log, to read in the page or download. **Delete** next to each removes that file; **Delete all logs** removes every log and report listed. Both ask first and are refused while a job is running. The audit trail (`actions.csv`) and the triage database are always kept, and folders left empty are removed |
| Settings | Every setting in the config file, as a form or as raw YAML |

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
