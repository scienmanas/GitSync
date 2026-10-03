"""Log wiring: the timestamped file in Logs/, the buffer the live UI renders
from, and the formatter that redacts credentials before anything is written.

Handlers are attached by configure_logging() rather than at import time, so
importing GitSync never leaves a stray log file behind. Every other module just
does `logger = logging.getLogger(__name__)` and lets the records propagate.
"""

import logging
import os
import sys
import threading
import time
import traceback
from collections import deque

from rich.console import Console

from . import config
from .redaction import redact

# Logs buffer for the UI (most recent N lines). How many of them the dashboard
# actually draws depends on the terminal - see ui.panel_line_count().
LOG_MAX_LINES = 300
logs_deque = deque(maxlen=LOG_MAX_LINES)
console = Console()

_log_file_path = None
_file_handler = None   # the handler writing _log_file_path, swapped per scheduled run

# Which repo the current worker thread is busy with (used as a log prefix)
_thread_state = threading.local()


def set_current_repo(repo_name) -> None:
    _thread_state.repo = repo_name


def current_repo():
    return getattr(_thread_state, "repo", None)


# Tags every line with the worker's repo and redacts the fully rendered record
# (message, args and traceback alike) so no handler ever sees a live token.


class SafeFormatter(logging.Formatter):
    def format(self, record) -> str:  
        if not hasattr(record, "repo_prefix"):
            repo = current_repo()
            record.repo_prefix = f"[{repo}] " if repo else ""
        return redact(super().format(record))


# Rich-aware logger handler that appends to logs_deque


class RichBufferHandler(logging.Handler):
    def __init__(self, buffer_deque) -> None:
        super().__init__()          # run Handler's own setup first
        self.buffer = buffer_deque  # then remember where to put lines

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = redact(record.getMessage())
        # Append to deque (keeps most recent lines). deque.append is atomic, so
        # workers can log while the UI thread renders.
        for line in msg.splitlines():
            self.buffer.append(line)


# Last line of defence: an uncaught crash must not dump a tokenised git command
# onto stderr. Every type goes through redact(), Ctrl-C included: a plain
# KeyboardInterrupt carries nothing secret, but one raised while another error
# is being handled prints that error's message too ("During handling of the
# above exception..."), and SIGTERM becomes a KeyboardInterrupt at whatever
# point the program happens to be - including inside an except block holding a
# tokenised URL. Printing it ourselves rather than delegating to
# sys.__excepthook__ does not change the exit code: the interpreter notes an
# unhandled KeyboardInterrupt before calling the hook, so it is still 130.


def _redacting_excepthook(exc_type, exc_value, exc_tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    sys.stderr.write(redact(text))

# Attach the handlers. Safe to call twice - the second call is a no-op.


def configure_logging() -> str:
    global _log_file_path
    if _log_file_path is not None:
        return _log_file_path

    # Ensure the logs directory exists
    os.makedirs(config.LOGS_FOLDER, exist_ok=True)
    # Generate log filename with timestamp
    _log_file_path = _new_log_path()

    formatter = SafeFormatter(
        "%(asctime)s %(levelname)s %(repo_prefix)s%(message)s")

    global _file_handler
    file_handler = logging.FileHandler(_log_file_path, mode="a")
    file_handler.setFormatter(formatter)
    _file_handler = file_handler

    # Add a rich UI handler for logs
    rich_handler = RichBufferHandler(logs_deque)
    rich_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(rich_handler)

    # Without a terminal (docker, cron, CI) there is no live dashboard to read,
    # and `docker logs` only ever sees stdout - so mirror the log there, one
    # line at a time. Same redacting formatter: a container log is at least as
    # public as the file.
    if not console.is_terminal:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        root.addHandler(stdout_handler)

    sys.excepthook = _redacting_excepthook

    # Replay whatever the env parsing complained about
    logger = logging.getLogger(__name__)
    for warning in config.STARTUP_WARNINGS:
        logger.warning(warning)

    prune_old_logs()
    return _log_file_path


def _new_log_path() -> str:
    return os.path.join(config.LOGS_FOLDER,
                        f"logs_{time.strftime('%Y%m%d_%H%M%S')}.txt")

# Give the next run a log file of its own. The scheduler calls this before each
# slot: it is one process that lives for months, and with a single file opened
# at startup that file would grow without end. Swapped on the main thread
# before the run starts, so no worker is mid-write. A no-op until
# configure_logging has run.


def start_new_log_file() -> str:
    # These two module-level names are reassigned below, so they must be
    # declared global - otherwise Python would treat them as new local names
    global _log_file_path, _file_handler
    root = logging.getLogger()
    # Nothing to swap if configure_logging has not run, or if our handler was
    # taken off the root logger by someone else (tests do this) - just report
    # where logging currently goes
    if _file_handler is None or _file_handler not in root.handlers:
        return log_file_path()

    new_path = _new_log_path()
    # The file name is stamped to the second, so two runs starting within the
    # same second would get the same name - then simply keep writing to it
    if new_path != _log_file_path:
        handler = logging.FileHandler(new_path, mode="a")
        # Same formatter as before, so the new file is redacted and prefixed
        # with the repo name exactly like the old one
        handler.setFormatter(_file_handler.formatter)
        # Add the new handler *before* removing the old one: for that instant
        # a record goes to both files, rather than to neither
        root.addHandler(handler)
        root.removeHandler(_file_handler)
        # Flush and release the old file - otherwise its handle stays open for
        # the life of the scheduler, one per run
        _file_handler.close()
        _file_handler, _log_file_path = handler, new_path

    # A natural moment to age out old files: once per run, with the new file
    # already in place (so it is the one prune_old_logs protects)
    prune_old_logs()
    return _log_file_path

# Delete our own log files (logs_*.txt) last modified more than
# LOG_RETENTION_DAYS ago - never the one being written, never anything else in
# the folder. Returns how many went. LOG_RETENTION_DAYS=0 keeps everything.


def prune_old_logs(now=None) -> int:
    # 0 (or less) means "keep every log"; and with no logs folder yet there is
    # nothing to prune
    days = config.LOG_RETENTION_DAYS
    if days <= 0 or not os.path.isdir(config.LOGS_FOLDER):
        return 0
    # Anything last modified before this moment goes. `now` is only passed by
    # tests, to fix the clock; 86400 is the number of seconds in a day.
    cutoff = (now if now is not None else time.time()) - days * 86400

    removed = 0
    for name in os.listdir(config.LOGS_FOLDER):
        # Only files we wrote ourselves (logs_<timestamp>.txt) - LOGS_DIR may
        # point at a folder that holds other things, and those are not ours
        if not (name.startswith("logs_") and name.endswith(".txt")):
            continue
        path = os.path.join(config.LOGS_FOLDER, name)
        # Never the file this run is writing, however old its timestamp looks
        # (a clock jump or a restored backup can make a fresh file seem
        # ancient). Deleting an open file does not stop the writes on Linux or
        # macOS - they go on into a file with no name, and the whole run's log
        # is lost without any error.
        if path == _log_file_path:
            continue
        try:
            # getmtime = last modified, in seconds since 1970 - the same scale
            # as cutoff, so a plain < compares them
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass    # gone already, or not ours to delete - not worth failing a run
    # Say so in the log, so a missing old file is never a mystery
    if removed:
        logging.getLogger(__name__).info(
            "Removed %d log file(s) older than %d days", removed, days)
    return removed

# Where the current run is logging to


def log_file_path() -> str:
    return _log_file_path or "(logging not configured)"
