# Quick start

## Requirements

| Need | Why |
|---|---|
| Docker and Docker Compose | ImagePipe ships as a container |
| A folder for ImagePipe's data | Holds the manifest database, logs and reports. Put it on fast storage that is backed up or snapshotted. |
| Read access to your photo folders | Sources are always mounted read-only |

No GPU is needed for Catalogue.

## 1. Get the code

Clone the repository, then work from its folder.

## 2. Create the config

Copy `config.example.yml` to `config/imagepipe.yml` and edit it. At minimum, set
one entry under `sources`. See [Configuration](configuration.md) for every key.

## 3. Create the compose file

Copy `compose.example.yml` to `compose.yml` and change the two host paths:

- the data folder, mounted at `/data`
- each source folder, mounted read-only (`:ro`) at the `path` you gave it in the config

## 4. Build and check

```bash
docker compose build
```

```bash
docker compose run --rm imagepipe status
```

`status` prints the config it loaded, the manifest location and each source.

## 5. Run a catalogue

```bash
docker compose run --rm imagepipe catalogue --source photos
```

Run it in the foreground (with `run`, not `up -d`) so the live progress shows on
your screen. It prints progress for each of its three phases, then a summary,
and writes reports to `reports_dir`. You can stop it with Ctrl+C at any time;
the next run carries on where it stopped.

## 6. Read the results

- `summary.md` in the report folder - totals by type and top-level folder.
- `by_folder.csv`, `by_extension.csv`, `extension_mismatches.csv`, `errors.csv`.
- For every single file in one CSV:

```bash
docker compose run --rm imagepipe export-csv --source photos
```
