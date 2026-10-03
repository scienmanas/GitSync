"""Lets you run the package directly: `python -m gitsync [command]`."""

import sys

from .cli import main

# Same contract as main.py: main() runs to completion and returns an exit code,
# sys.exit passes it on to the shell.
if __name__ == "__main__":
    sys.exit(main())
