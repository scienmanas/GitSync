# GitSync, on Alpine. Two stages so uv and the build metadata never reach the
# runtime image - what ships is python + git + the virtualenv + this project.

FROM python:3.14-alpine AS builder

# uv resolves from uv.lock, so the container gets byte-for-byte the versions CI
# tested. Pinned rather than :latest - a build should not change on its own.
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

# Only the lock files, so this layer is cached until a dependency actually moves
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev


# Must be the same Python as the builder: the venv copied below is tied to it
FROM python:3.14-alpine

# Set by the publish workflow from the tag it was asked for; "dev" for a plain
# local `docker build`. Baked in so a running container can say what it is.
ARG VERSION=dev

# What ghcr.io reads to link the package to this repository, and what
# `docker inspect` shows anyone trying to work out which image they are running.
LABEL org.opencontainers.image.title="GitSync" \
      org.opencontainers.image.description="Back up your GitHub repositories - mirror them to GitLab with full history or zip them up (full git mirrors or just the code). Run it once or keep it running on a cron schedule (CRON_SCHEDULE). Runs unprivileged with everything in the /data volume - masks tokens in every log line and never runs two backups at once. Setup and docs: https://github.com/scienmanas/GitSync" \
      org.opencontainers.image.source="https://github.com/scienmanas/GitSync" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# git does the actual mirroring; ca-certificates is what makes https work at
# all; tzdata lets TZ mean something, which a cron schedule depends on.
RUN apk add --no-cache git ca-certificates tzdata

# An unprivileged fixed uid: predictable for `chown 1000:1000` on a bind mount.
RUN addgroup -g 1000 -S gitsync \
    && adduser -u 1000 -S -G gitsync -h /home/gitsync gitsync

WORKDIR /app
COPY --from=builder --chown=gitsync:gitsync /app/.venv /app/.venv
COPY --chown=gitsync:gitsync main.py ./
COPY --chown=gitsync:gitsync gitsync/ ./gitsync/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The venv first on PATH, so `python` is the one with the dependencies in it
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GITSYNC_VERSION=${VERSION} \
    BACKUP_DIR=/data/repos-backup \
    ARCHIVE_DIR=/data/archives \
    LOGS_DIR=/data/logs

# Everything worth keeping lives under /data - mirrors, archives and logs - so a
# single volume is all that stands between a restart and a full re-clone.
RUN mkdir -p /data/repos-backup /data/archives /data/logs \
    && chown -R gitsync:gitsync /data
VOLUME ["/data"]

USER gitsync

# The mirrors are written by this uid, but a volume restored from elsewhere may
# carry different ownership; without this git would refuse it as "dubious".
RUN git config --global --add safe.directory '*'

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# No argument means one sync - unless CRON_SCHEDULE is set, which the entrypoint
# reads as "stay up and keep doing it".
CMD []
