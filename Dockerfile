FROM python:3.12-slim

LABEL org.opencontainers.image.title="ImagePipe" \
      org.opencontainers.image.description="Repeatable, never-delete photo ingest pipeline" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[heif]"

ENV IMAGEPIPE_CONFIG=/config/imagepipe.yml \
    PYTHONUNBUFFERED=1 \
    SQLITE_TMPDIR=/data/tmp

ENTRYPOINT ["tini", "--", "imagepipe"]
CMD ["status"]
