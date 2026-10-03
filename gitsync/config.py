"""Everything GitSync reads from the environment.

Deliberately free of logging/UI imports so every other module can depend on it
without an import cycle. Anything worth complaining about while parsing the env
lands in STARTUP_WARNINGS and is replayed once the logger exists.
"""

import os

from dotenv import load_dotenv

# Laod Env variables
load_dotenv()

# Problems spotted while reading the env, replayed once the logger exists
STARTUP_WARNINGS = []

# Values shipped in .env.example - treated as "not configured"
PLACEHOLDER_VALUES = {
    "your-github-username",
    "your-github-token",
    "your-gitlab-username",
    "your-gitlab-token",
    "your-gitlab-group",
}

# Read an int from the env, falling back to the default when it is unusable


def env_int(name, default, minimum=None, maximum=None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip()) # For CPU usage
    except ValueError:
        STARTUP_WARNINGS.append(
            f"{name}={raw.strip()!r} is not a number, falling back to {default}")
        return default
    # Clamp rather than reject: an out-of-range value (e.g. CPU_LOAD_PERCENT=500)
    # would otherwise flow straight into thread-pool sizing downstream. Clamping
    # here, once, means the user sees a warning instead of the tool silently
    # doing something nonsensical (or sync.py's own clamp masking the bad input).
    if minimum is not None and value < minimum:
        STARTUP_WARNINGS.append(
            f"{name}={value} is below the minimum of {minimum}, clamping")
        value = minimum
    if maximum is not None and value > maximum:
        STARTUP_WARNINGS.append(
            f"{name}={value} is above the maximum of {maximum}, clamping")
        value = maximum
    return value

# Read true/false from the env (1/0, yes/no and on/off work too, in any case),
# falling back to the default - with a warning - when it is none of those


def env_bool(name, default) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    STARTUP_WARNINGS.append(
        f"{name}={raw.strip()!r} is not true/false, falling back to {default}")
    return default

# Read a comma-separated list from the env. None (rather than []) means "not
# set", so callers can tell "leave unset" apart from "set to an empty list"
# and fall back to their own default.


def env_list(name):
    raw = os.getenv(name)
    if raw is None or not raw.strip(): # Check empty thing
        return None
    return tuple(item.strip() for item in raw.split(",") if item.strip())


#  Configs
GITHUB_USER = os.getenv("GITHUB_USER", "your-github-username")
GITHUB_TOKEN = os.getenv(
    "GITHUB_TOKEN", "your-github-token")   # repo read access
GITLAB_USER = os.getenv("GITLAB_USER", "your-gitlab-username")
# Optional, so the untouched .env.example placeholder means "no group" rather
# than a lookup of a group literally called "your-gitlab-group"
GITLAB_GROUP = os.getenv("GITLAB_GROUP") or None
if GITLAB_GROUP in PLACEHOLDER_VALUES:
    GITLAB_GROUP = None
# api + write_repository (or api)
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "your-gitlab-token")
REPO_VISIBILITY = os.getenv("REPO_VISIBILITY", "auto")
# Back up your forks too (GitHub marks them "fork": true). They are often just
# copies of someone else's project - possibly a big one, cloned with its whole
# history - so they can be left out. Applies to sync and archive alike.
INCLUDE_FORKS = env_bool("INCLUDE_FORKS", True)
# GitLab protects each project's default branch and refuses force pushes to it,
# so a GitHub repo whose history was rewritten (amend, rebase, force push) can't
# be mirrored. With this on, a push rejected for that reason makes GitSync turn
# on "allowed to force push" for that project's protected branches and retry
# once. Off by default: it changes a setting in your GitLab projects.
GITLAB_ALLOW_FORCE_PUSH = env_bool("GITLAB_ALLOW_FORCE_PUSH", False)
PER_PAGE = 100
BACKUP_DIR = os.getenv("BACKUP_DIR", "./repos-backup")
ARCHIVE_DIR = os.getenv("ARCHIVE_DIR", "./archives")
# Folder names stripped from 'archive --mode code' snapshots. None means "not
# set" - archive.py falls back to its own built-in default list.
CODE_ARCHIVE_EXCLUDE_DIRS = env_list("CODE_ARCHIVE_EXCLUDE_DIRS")
GITLAB_URL = "https://gitlab.com"
SLEEP_BETWEEN_API = 0.5  # seconds
# How long a GitHub/GitLab API call may wait on the server, in seconds. requests
# waits forever by default, so one dead connection would freeze an unattended
# run - and the schedule behind it - for good.
HTTP_TIMEOUT = 30
# A git clone/fetch/push is aborted once it has moved less than
# GIT_LOW_SPEED_LIMIT bytes/s for GIT_LOW_SPEED_TIME seconds. A stall, not a
# duration, so a big repo that is merely slow is never cut off.
GIT_LOW_SPEED_LIMIT = 1000
GIT_LOW_SPEED_TIME = 120

# Parallelism. Repos are synced concurrently; how many at a time is either
# pinned with MAX_WORKERS or derived from the share of the CPU we may use.
CPU_LOAD_PERCENT = env_int("CPU_LOAD_PERCENT", 70, minimum=1, maximum=100)
MAX_WORKERS = env_int("MAX_WORKERS", 0, minimum=0)

# Repo root (this file lives in gitsync/), so the logs land next to the project
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# LOGS_DIR matters in a container: the default sits inside the image, which is
# thrown away on every restart, so the compose file points it at the volume.
LOGS_FOLDER = os.getenv("LOGS_DIR") or os.path.join(PROJECT_DIR, "Logs")
# Log files older than this many days are deleted (each run writes its own
# file, so without a limit a long-running schedule fills the volume). 0 keeps
# every log forever.
LOG_RETENTION_DAYS = env_int("LOG_RETENTION_DAYS", 30, minimum=0)

# Which credentials are still missing or left at their placeholder value. The
# GitLab pair only matters when something is actually pushed there - fetching
# and archiving work with a GitHub token alone.


def missing_credentials(gitlab=True):
    # "Missing" covers two cases: the var is empty/unset (`not value`), or it
    # was never touched and is still the literal placeholder text shipped in
    # .env.example (`value in PLACEHOLDER_VALUES`). Catching the latter is what
    # lets sync.py fail fast with a clear message instead of authenticating
    # against GitHub/GitLab as the string "your-github-token" and failing with
    # a confusing 401 partway through a run.
    required = [("GITHUB_USER", GITHUB_USER), ("GITHUB_TOKEN", GITHUB_TOKEN)]
    if gitlab:
        required += [("GITLAB_USER", GITLAB_USER), ("GITLAB_TOKEN", GITLAB_TOKEN)]
    return [name for name, value in required
            if not value or value in PLACEHOLDER_VALUES]
