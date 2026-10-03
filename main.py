"""GitSync entry point.

The real code lives in the gitsync/ package - this file stays at the repo root
so `python main.py`, build.sh and the cron job written by deploy.sh keep working
exactly as before. `python -m gitsync` does the same thing.
"""

import sys

from gitsync.cli import main

# Run the programme. Python evaluates main() first, so the whole sync - thread
# pool and all - has already finished by the time sys.exit is called with the
# code it returned (0 fine, 1 something failed, 130 Ctrl-C, 2 bad arguments).
# sys.exit only raises SystemExit, so finally blocks and atexit handlers still
# run; it never cuts a worker short. Without it the return value would be
# dropped and the process would always report success to cron and CI.
#
# The __name__ == "__main__" guard only runs main() when this file is executed
# directly (`python main.py`), not when it's imported. On import, Python sets
# __name__ to the module's name ("main") instead of "__main__", so merely
# importing this file (e.g. `import main` from a test) can't trigger a sync
# as a side effect - main() only fires when the file is run as a script.
if __name__ == "__main__":
    sys.exit(main())
