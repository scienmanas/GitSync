"""Running git, with its output streamed into the log."""

import logging
import os
import subprocess
import threading

from . import config
from .redaction import redact

logger = logging.getLogger(__name__)

# The most telling error line from the last git command on this thread, for the
# end-of-run summary ("why did this repo fail?"). Per thread, because each
# worker runs its own git commands in parallel.
_last_error = threading.local()

# Which lines say *why* git failed, best first. For a rejected push git prints
#   remote: GitLab: You are not allowed to force push code to a protected branch
#    ! [remote rejected] main -> main (pre-receive hook declined)
#   error: failed to push some refs to '...'
# and the first of those is the one worth showing.
_ERROR_MARKERS = (("remote: GitLab:", 4), ("fatal:", 3), ("! [remote rejected]", 2),
                  ("remote: error:", 2), ("error:", 1))


def last_git_error():
    """The most telling error line of the last git command run on this thread
    (already redacted), or None if it printed none."""
    return getattr(_last_error, "line", None)


def _remember_error(line) -> None:
    text = line.strip()
    for marker, rank in _ERROR_MARKERS:
        if text.startswith(marker):
            if rank > getattr(_last_error, "rank", 0):
                _last_error.line, _last_error.rank = redact(text), rank
            return

# Runs cmd commands


def run(cmd, cwd=None, check=True) -> None:
    # The command carries the auth URL, so only ever log/raise the masked form
    safe_cmd = [redact(part) for part in cmd]
    logger.info("RUN: %s (cwd=%s)", " ".join(safe_cmd), cwd or ".")
    _last_error.line, _last_error.rank = None, 0     # a fresh command, a fresh slate
    # Never let git block a background/parallel run on a credential prompt
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Nor on a connection that has stopped moving - see config.GIT_LOW_SPEED_*
    env["GIT_HTTP_LOW_SPEED_LIMIT"] = str(config.GIT_LOW_SPEED_LIMIT)
    env["GIT_HTTP_LOW_SPEED_TIME"] = str(config.GIT_LOW_SPEED_TIME)
    # Popen, not subprocess.run: it starts the child and returns straight away,
    # so the output can be read while git is still working. run() would block
    # until the end and hand back one blob - no progress in the panel for the
    # several minutes a big clone takes.
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        # Send the output to us rather than to the terminal, where it would
        # scribble over the live dashboard instead of going into the log.
        stdout=subprocess.PIPE,
        # Fold stderr into the same pipe. git reports progress on stderr, so
        # without this most of what we want would be missed - and one pipe
        # keeps the two streams in the order git actually wrote them.
        stderr=subprocess.STDOUT,
        text=True,          # decode the bytes to str for us
        bufsize=1,          # line buffered: hand us each line as it completes
        universal_newlines=True,   # the old name for text=True; same thing
        env=env
    )
    # Stream lines as they appear
    try:
        # For the type checker: stdout is Optional on Popen, and we did pass
        # PIPE, so it is really there. Not a runtime check of anything.
        assert proc.stdout is not None
        # Iterating a pipe blocks until the next line arrives and ends only when
        # git closes it, i.e. when git exits - so this loop is the whole run,
        # pacing itself to git's output rather than spinning.
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")  # Remove the trailing newline, which is already added by logger.info()
            # log each line - will go to file and to the rich UI buffer
            logger.info(line)
            _remember_error(line)
        # The loop ends at end-of-pipe, which is a moment before the process is
        # actually reaped; wait() is what fills in returncode below.
        proc.wait()
    except Exception:
        # Reading blew up (or the caller went away) - kill the child rather than
        # leave a git process cloning into a folder nobody is watching any more,
        # then re-raise so the failure still reaches process_repo and the repo is
        # counted as failed.
        proc.kill()
        raise
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, safe_cmd)
