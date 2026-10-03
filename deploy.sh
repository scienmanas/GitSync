#!/bin/bash
# GitSync: run it automatically with cron.
#
#   bash deploy.sh
#
# Asks what to run (sync to GitLab, or a zip with archive) and how often, then
# adds one crontab entry - replacing any earlier GitSync entry, so re-running
# this script changes the schedule instead of adding a second, clashing job.

set -e

# Absolute paths: cron starts jobs in your home folder, not here
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
PYTHON_PATH="$PROJECT_DIR/.venv/bin/python"
MAIN_PATH="$PROJECT_DIR/main.py"
LOG_PATH="$PROJECT_DIR/shell_logs.txt"

if [ ! -x "$PYTHON_PATH" ] || [ ! -f .env ]; then
    echo "Error: run 'bash build.sh' first - it creates .env and the .venv this needs."
    exit 1
fi

echo "==== GitSync Cron Job Setup ===="
echo

# --- what to run ------------------------------------------------------------
# sync needs GitLab; without it, the only thing that can succeed is archive
GITLAB_TOKEN="$(grep -E '^GITLAB_TOKEN=' .env | tail -1 | cut -d= -f2-)"
if [ -n "$GITLAB_TOKEN" ] && [ "$GITLAB_TOKEN" != "your-gitlab-token" ]; then
    DEFAULT_CHOICE=1
else
    DEFAULT_CHOICE=2
    echo "(No GitLab token in .env, so 'sync' would fail - archive is the default.)"
fi
echo "What should run?"
echo "  1) sync                  - mirror your repos to GitLab"
echo "  2) archive --mode full   - zip everything, full history"
echo "  3) archive --mode code   - zip just the latest code, small"
read -p "Choice [$DEFAULT_CHOICE]: " CHOICE
case "${CHOICE:-$DEFAULT_CHOICE}" in
    1) COMMAND="sync" ;;
    2) COMMAND="archive --mode full" ;;
    3) COMMAND="archive --mode code" ;;
    *) echo "Error: choose 1, 2 or 3."; exit 1 ;;
esac
echo

# --- how often --------------------------------------------------------------
echo "How often? A cron expression: minute hour day month weekday"
echo "  '0 2 * * *'   -> every day at 02:00"
echo "  '0 * * * *'   -> every hour"
echo "  '0 3 * * 0'   -> every Sunday at 03:00"
echo "More often than hourly is not supported: a run walks your whole account."
echo "Help with the format: https://crontab.guru"
read -p "Cron schedule [0 2 * * *]: " CRON_SCHEDULE
CRON_SCHEDULE="${CRON_SCHEDULE:-0 2 * * *}"

# Check it with GitSync's own parser - the same rules its scheduler uses
if ! "$PYTHON_PATH" -c '
import sys
from gitsync.schedule import CronError, check_min_interval, parse_cron
try:
    check_min_interval(parse_cron(sys.argv[1]))
except CronError as e:
    print(f"Error: {e}")
    sys.exit(1)
' "$CRON_SCHEDULE"; then
    exit 1
fi

# --- install the cron job ----------------------------------------------------
# cron runs with a bare PATH (/usr/bin:/bin), so put the folder of the git you
# use in front of it - e.g. Homebrew's git in /opt/homebrew/bin
GIT_PATH="$(command -v git || true)"
if [ -z "$GIT_PATH" ]; then
    echo "Error: git is not installed (or not on your PATH)."
    exit 1
fi
GIT_DIR_PATH="$(dirname "$GIT_PATH")"

# cd first, so the default ./repos-backup and ./archives are the project's own
# folders - cron would otherwise create them in your home folder
CRON_CMD="$CRON_SCHEDULE /bin/sh -c 'cd \"$PROJECT_DIR\" && PATH=\"$GIT_DIR_PATH:/usr/bin:/bin\" && /bin/echo \"Cron job triggered at \$(date)\" >> \"$LOG_PATH\" && \"$PYTHON_PATH\" \"$MAIN_PATH\" $COMMAND >> \"$LOG_PATH\" 2>&1'"

# Drop any earlier GitSync entry (any line running this main.py), keep the rest.
# crontab -l's stderr goes to /dev/null so "no crontab" never lands in the file.
EXISTING="$(crontab -l 2>/dev/null | grep -F -v "$MAIN_PATH" || true)"
if crontab -l 2>/dev/null | grep -F -q "$MAIN_PATH"; then
    echo "Replacing your existing GitSync cron job."
fi
{ [ -n "$EXISTING" ] && echo "$EXISTING"; echo "$CRON_CMD"; } | crontab -

echo
echo "Done! '$COMMAND' runs on: $CRON_SCHEDULE"
echo
echo "Output of each run:   $LOG_PATH"
echo "Detailed logs:        $PROJECT_DIR/Logs/"
echo "See your cron jobs:   crontab -l"
echo "Change the schedule:  bash deploy.sh   (replaces this job)"
echo "Remove it:            crontab -e   and delete the line with main.py"
