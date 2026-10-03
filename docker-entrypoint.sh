#!/bin/sh
# GitSync container entrypoint.
#
#   docker run gitsync                      one sync, then exit
#                                           (or scheduled, if CRON_SCHEDULE is set)
#   docker run gitsync sync                 one sync, then exit
#   docker run gitsync archive --mode code  one archive, then exit
#   docker run gitsync cron                 stay up, sync on CRON_SCHEDULE
#   docker run -it gitsync sh               a shell, for poking around
#
# Everything is configured through environment variables, so nothing has to be
# baked into the image and `docker run --env-file .env` is all a user needs.
#
# The schedule is kept by `main.py schedule`, not by crond: this image runs as an
# unprivileged user, and busybox crond will not start a job unless it is root.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/data/repos-backup}"
ARCHIVE_DIR="${ARCHIVE_DIR:-/data/archives}"
LOGS_DIR="${LOGS_DIR:-/data/logs}"
CRON_SCHEDULE="${CRON_SCHEDULE:-}"
CRON_COMMAND="${CRON_COMMAND:-sync}"
RUN_ON_START="${RUN_ON_START:-true}"
export BACKUP_DIR ARCHIVE_DIR LOGS_DIR

say() { echo "gitsync: $*"; }
die() { echo "gitsync: $*" >&2; exit 1; }

# The data directories have to exist and be ours before anything else happens.
# A bind mount owned by another uid is the classic docker surprise, so fail here
# with an explanation rather than halfway through cloning someone's account.
prepare_directories() {
    for dir in "$BACKUP_DIR" "$ARCHIVE_DIR" "$LOGS_DIR"; do
        mkdir -p "$dir" 2>/dev/null || die "cannot create $dir (uid $(id -u))"
        [ -w "$dir" ] || die "$dir is not writable by uid $(id -u).
         It is probably a bind mount owned by another user - either
         chown it on the host or start the container with --user."
    done
}

mode="${1:-}"
case "$mode" in
    sync|archive|schedule|cron) shift ;;
    "")     mode="" ;;
    -*)     exec python /app/main.py "$@" ;;   # a flag, e.g. --help: it is the CLI's
    *)      exec "$@" ;;   # not ours at all: run it verbatim (sh, python, git...)
esac

# No command and no schedule means "just back me up once"; with a schedule set,
# staying up and keeping to it is obviously what was meant.
if [ -z "$mode" ]; then
    if [ -n "$CRON_SCHEDULE" ]; then mode="cron"; else mode="sync"; fi
fi

prepare_directories

if [ "$mode" = "cron" ]; then
    [ -n "$CRON_SCHEDULE" ] || die "cron mode needs CRON_SCHEDULE, e.g. -e CRON_SCHEDULE='0 2 * * *'"

    say "schedule: $CRON_SCHEDULE (TZ=${TZ:-UTC}), command: $CRON_COMMAND"
    say "data: $BACKUP_DIR | archives: $ARCHIVE_DIR | logs: $LOGS_DIR"

    # A restart is a normal event for a container and the next slot may be hours
    # away, so unless told otherwise, catch up straight away. The mirrors are in
    # a volume, so that is a fetch, not a fresh clone of the whole account.
    on_start=""
    [ "$RUN_ON_START" = "true" ] && on_start="--run-on-start"

    # exec, so the scheduler is PID 1 and `docker stop` signals it directly:
    # SIGTERM lands on the process that knows how to stop a sync tidily.
    # CRON_COMMAND is deliberately unquoted - it may carry flags, e.g.
    # CRON_COMMAND="archive --mode code", which have to split into words.
    # set -f stops that split from also glob-expanding: without it
    # "archive --exclude *.pdf" would hand over whatever files in /app happen
    # to match instead of the pattern. (Quotes inside the variable are not
    # parsed either - write --exclude *.pdf, not --exclude '*.pdf'.)
    set -f
    exec python /app/main.py schedule --cron "$CRON_SCHEDULE" $on_start $CRON_COMMAND
fi

exec python /app/main.py "$mode" "$@"
