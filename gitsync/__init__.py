"""GitSync - mirror your GitHub repositories to GitLab and archive them locally.

Layout:
    config.py          env/.env settings and the credential sanity check
    redaction.py       masks tokens before anything is logged or printed
    logging_setup.py   log file, live-UI buffer, per-repo log context
    process.py         runs git and streams its output into the log
    github_api.py      GitHub API calls
    gitlab_api.py      GitLab API calls
    ui.py              shared rich widgets (progress bar, live dashboard)
    sync.py            the sync command (parallel mirror + push)
    archive.py         the archive command (zip the local mirrors)
    cli.py             argument parsing and dispatch

Nothing here runs at import time beyond reading the environment - call
cli.main() (or `python main.py`) to actually do something.
"""

__version__ = "2.0.0"
