"""One GitSync run at a time per backup folder.

Two runs touching the same mirrors at once - the scheduled container plus a
`docker compose run --rm gitsync sync` one-off, or a manual `main.py sync`
while cron's is still going - would have two git processes fetching into, and
recloning over, the same folders. So every sync and archive takes a lock file in
the backup folder first, and a run that finds it taken does not start.

The lock is an OS-level flock on .gitsync.lock, not just the file being there:
the kernel releases it the moment the holding process exits - even on a crash
or SIGKILL - so a dead run can never leave a stale lock behind. Containers that
share the /data volume share the kernel, so they see each other's locks too.
"""

import contextlib
import fcntl
import os
import time

LOCK_NAME = ".gitsync.lock"

# Exit code for "skipped - another run holds the lock": 75 is EX_TEMPFAIL from
# sysexits.h, "temporary failure, try again later". Kept apart from 1 (a run
# that failed) so a script or `docker logs` reader can tell the two apart.
EXIT_BUSY = 75


class RunInProgress(Exception):
    """Another GitSync run holds the lock. str() describes that run."""


# Hold the lock for the duration of the `with` block. `what` describes this run
# ("sync", "archive --mode code") and is written into the lock file, so a run
# that finds the lock taken can say who has it and since when.


@contextlib.contextmanager
def exclusive_run(backup_dir, what):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, LOCK_NAME)
    # "a+" opens for read and write without wiping the holder's note before we
    # know whether the lock is ours to take
    lock_file = open(path, "a+")
    try:
        try:
            # LOCK_NB: fail at once instead of waiting for the other run to end
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.seek(0)
            holder = lock_file.read().strip() or "another GitSync run"
            raise RunInProgress(holder) from None

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{what} (pid {os.getpid()}, started "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')})")
        lock_file.flush()
        try:
            yield
        finally:
            lock_file.seek(0)
            lock_file.truncate()      # no stale note for the next reader
            lock_file.flush()
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
