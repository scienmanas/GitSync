"""Command line surface: `sync` (the default) and `archive`."""

import argparse
import logging
import signal
import time

from rich.markup import escape

from . import config
from .archive import run_archive
from .runlock import EXIT_BUSY, RunInProgress, exclusive_run
from .logging_setup import (configure_logging, console, log_file_path,
                            start_new_log_file)
from .schedule import CronError, run_schedule
from .sync import run_sync

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="GitSync - mirror your GitHub repositories to GitLab and bundle them into a zip backup.")
    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser(
        "sync", help="Mirror every GitHub repo to GitLab (default command). "
                     "Needs both GitHub and GitLab credentials.")
    sync_parser.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help="Exact number of repos to sync in parallel. Overrides MAX_WORKERS and --cpu-load.")
    sync_parser.add_argument(
        "--cpu-load", type=int, default=None, metavar="PERCENT",
        help=f"Share of the CPU cores to keep busy, 1-100 (default: {config.CPU_LOAD_PERCENT}).")

    archive_parser = subparsers.add_parser(
        "archive", help="Fetch the latest from GitHub, then bundle every repo into a "
                        "single zip file. Needs a GitHub token only - GitLab is not used.")
    archive_parser.add_argument(
        "--mode", choices=("full", "code"), default="full",
        help="'full' keeps the complete git mirror - all history plus everything committed, "
             "including vendored node_modules/site-packages. 'code' keeps only a snapshot of the "
             "latest commit on the default branch, with dependency and build folders "
             "stripped out (default: full).")
    archive_parser.add_argument(
        "--compression", type=int, default=6, choices=range(0, 10), metavar="{0-9}",
        help="Deflate level: 0 stores without compressing, 9 compresses hardest (default: 6).")
    archive_parser.add_argument(
        "-o", "--output", default=None, metavar="PATH",
        help=f"Zip file to write (default: {config.ARCHIVE_DIR}/gitsync-<mode>-<timestamp>.zip).")
    archive_parser.add_argument(
        "--backup-dir", default=config.BACKUP_DIR, metavar="PATH",
        help=f"Folder holding the mirrored repos (default: {config.BACKUP_DIR}).")
    archive_parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="Extra glob to skip in 'code' mode. Repeatable.")
    archive_parser.add_argument(
        "--no-fetch", action="store_true",
        help="Zip the mirrors already on disk without refreshing them from GitHub "
             "first (works offline, no token needed).")
    archive_parser.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help="Repos to fetch in parallel before archiving (same as sync --workers).")

    schedule_parser = subparsers.add_parser(
        "schedule",
        help="Stay running and repeat a command on a cron schedule (what the "
             "Docker image runs).")
    schedule_parser.add_argument(
        "--cron", required=True, metavar="EXPR",
        help="Cron expression, e.g. '0 2 * * *' or '@daily'. At most hourly - "
             "anything more frequent (e.g. '*/15 * * * *') is refused.")
    schedule_parser.add_argument(
        "--run-on-start", action="store_true",
        help="Run once immediately instead of waiting for the first slot.")
    schedule_parser.add_argument(
        "command_args", nargs=argparse.REMAINDER, metavar="COMMAND",
        help="What to run on each slot, with its flags (default: sync).")

    return parser

# `docker stop` and systemd send SIGTERM, which by default kills the process on
# the spot - workers abandoned mid-clone, a half-written zip left behind. Turning
# it into a KeyboardInterrupt reuses the Ctrl-C path we already handle: the pool
# is drained, the partial archive is deleted and we exit 130. Signal handlers
# only ever run on the main thread, which is exactly where that path lives.
#
# In the container this is not a nicety but the only way to stop cleanly at all:
# the entrypoint execs us, so we are PID 1, and Linux does not apply a signal's
# default action to PID 1 - a SIGTERM with no handler installed is dropped. With
# nothing registered here `docker stop` would wait out its grace period and then
# send SIGKILL, which cannot be caught, in the middle of whatever a worker was
# writing.
#
# Only SIGTERM needs registering: Python already installs a SIGINT handler that
# raises KeyboardInterrupt, which is why Ctrl-C works without our help.


def install_signal_handlers() -> None:
    def terminate(*_):   # signal handlers are called with (signum, frame)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)


# Run one command as if it had been typed on the command line. The scheduler
# calls this on every slot, so a scheduled sync goes down exactly the same path
# as a manual one - no second implementation to keep in step.


def run_once(command_args) -> int:
    args = build_arg_parser().parse_args(command_args)
    if (args.command or "sync") == "schedule":
        raise SystemExit("A schedule cannot schedule another schedule.")
    return run_command(args)


# Run sync or archive - holding the backup folder's lock (see runlock.py), so
# two runs never work on the same mirrors at once. If another run already holds
# it, this one does not start: it says who has it and exits EXIT_BUSY (75).
# Both the command line and every scheduled slot come through here.


def run_command(args) -> int:
    command = args.command or "sync"
    if command == "archive":
        backup_dir = args.backup_dir
        what = "archive --mode " + args.mode
    else:
        backup_dir = config.BACKUP_DIR
        what = "sync"
    try:
        with exclusive_run(backup_dir, what):
            if command == "archive":
                return run_archive_command(args)
            return run_sync(workers_override=getattr(args, "workers", None),
                            cpu_load_override=getattr(args, "cpu_load", None))
    except RunInProgress as busy:
        message = (f"Another GitSync run is still going ({busy}) - "
                   f"skipping this {command}. It starts again next time.")
        logger.warning(message)
        console.print(f"[yellow]{escape(message)}[/yellow]")
        return EXIT_BUSY


# The archive command: bring the mirrors up to date, then zip them. A failed
# fetch does not stop the zip - the mirrors on disk are still the best backup
# there is - but it does fail the run, so nobody mistakes a stale zip for a
# fresh one.


def run_archive_command(args) -> int:
    fetch_status = 0
    if not args.no_fetch:
        fetch_status = run_sync(workers_override=args.workers, push=False,
                                backup_dir=args.backup_dir)
        if fetch_status == 130:   # interrupted - stop here, zip nothing
            return 130
        if fetch_status != 0:
            logger.warning("Fetch from GitHub was incomplete - archiving the "
                           "mirrors as they are.")
    archive_status = run_archive(mode=args.mode, compression=args.compression,
                                 output=args.output, backup_dir=args.backup_dir,
                                 excludes=args.exclude)
    return archive_status or fetch_status


# One scheduled run: its own log file, then the command. The scheduler is one
# process that runs for months, so without a fresh file per run its single log
# would grow forever - this way a scheduled run is logged exactly like a
# manual one, and old files age out (LOG_RETENTION_DAYS).


def run_scheduled_slot(command_args) -> int:
    start_new_log_file()
    return run_once(command_args)


# The schedule command itself: what to repeat, and how often


def run_scheduled(args) -> int:
    # `--` is how argparse.REMAINDER is usually fed; drop it if it is there
    command_args = [part for part in args.command_args if part != "--"] or ["sync"]
    # Check the command once, up front: a typo'd flag or a nested schedule is a
    # setup mistake that should stop us now, not fail quietly on every slot
    # (run_schedule keeps going after a failed slot, by design)
    nested = build_arg_parser().parse_args(command_args)   # bad flags exit 2 here
    if (nested.command or "sync") == "schedule":
        logger.error("A schedule cannot schedule another schedule.")
        console.print("[red]A schedule cannot schedule another schedule.[/red]")
        return 2
    try:
        return run_schedule(
            expression=args.cron, description=" ".join(command_args),
            runner=lambda: run_scheduled_slot(command_args),
            run_on_start=args.run_on_start)
    except CronError as e:
        logger.error("Bad --cron value: %s", e)
        console.print(f"[red]Bad --cron value: {escape(str(e))}[/red]")
        return 2


# Main function. Returns the exit code instead of calling sys.exit() itself: the
# entry point decides what to do with it, and the tests can assert on a plain
# int rather than catching SystemExit around every call.


def main(argv=None) -> int:
    parser = build_arg_parser()
    # No arguments at all still means "sync", the way it always has
    args = parser.parse_args(argv if argv is not None else None)
    command = args.command or "sync"

    configure_logging()
    install_signal_handlers()
    logger.info(
        "####################### logger Started ############################")
    logger.info("#################### Timestamp: %s ######################",
                time.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Command: %s", command)

    try:
        # For archive mode
        # For scheduled mode - runs one of the others, over and over
        if command == "schedule":
            return run_scheduled(args)
        # sync or archive, one at a time per backup folder
        return run_command(args)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130
    except Exception as e:
        logger.exception("Something went wrong: %s", e)
        console.print(
            f"[red]Something went wrong - see {escape(log_file_path())} for details.[/red]")
        return 1
