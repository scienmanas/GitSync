# GitSync 🚀🦸‍♂️

😱 Ever had nightmares about losing your precious GitHub repos? Or just want to flex with a backup on GitLab? GitSync is here to save your code (and your sanity)! It mirrors all the GitHub repositories you own to GitLab, keeping every commit, every embarrassing typo, and every moment of genius. 💾✨

> **Note:** 🖼️ GitLab may show your activity a bit differently, so your contribution graph might look like modern art. But don’t worry, your commit history is safe! 🛡️

## Features ✨

- Mirrors every GitHub repository you own to GitLab, including every commit (even the ones you regret). Organization repos are coming in [3.0.0](#coming-in-300-). 🕵️‍♂️
- Works for personal and group namespaces on GitLab. Share the love (or the blame). 🤝
- Syncs several repos in parallel, sized to the share of your CPU you're willing to hand over. 🏎️
- Bundles every repo into one zip file, fetched fresh from GitHub first, either with everything committed or with dependency folders stripped out. No GitLab account needed for that. 🗜️
- Runs on a schedule in Docker and is safe to leave alone: one run at a time, a failed run retried at the next slot, old logs cleaned up. ⏰
- Never writes your tokens to the logs or the terminal — everything credential-shaped is masked before it is printed. 🔒
- Provides progress and logs with a rich UI, so you can watch your backup in style. 📊🎬
- Compatible with macOS and Linux. Windows? Maybe someday. (We like 🐧 penguins and 🍏 apples.)

## Quick start 🏁

The easiest way: run it on your own machine. You need [git](https://git-scm.com/) and [uv](https://docs.astral.sh/uv/getting-started/installation/) (0.12.22 or newer) — uv fetches Python for you.

```sh
git clone https://github.com/scienmanas/GitSync.git
cd GitSync
bash build.sh                       # asks for your GitHub token (and GitLab, optionally), installs everything
uv run python main.py archive       # fetch all your repos and zip them - GitHub only
uv run python main.py               # ...or mirror them to GitLab, if you gave a GitLab token
bash deploy.sh                      # optional: run it automatically every night (or hourly, weekly...)
```

That's it. `build.sh` only asks what it needs — a GitHub user and token, GitLab if you want to push there — and leaves everything else on sensible defaults (`Advanced settings?` lets you change them). Prefer to see every setting? Follow [Installation](#installation-) below instead. Want it in a container? See [Docker](#docker-).

## Installation 🛠️

Setting things up by hand, step by step (this is what `build.sh` does for you):


1. **Clone this masterpiece:**

   ```sh
   git clone https://github.com/scienmanas/GitSync.git
   cd GitSync
   ```

2. **Install dependencies (because Python loves packages):**

   GitSync uses [`uv`](https://docs.astral.sh/uv/) **0.12.22 or newer** for dependency management. Install it first if you don't have it (see the [uv install guide](https://docs.astral.sh/uv/getting-started/installation/)) — or, if yours is older, `uv self update` — then:

   ```sh
   uv sync
   ```

   This creates `.venv/` and installs everything pinned in `uv.lock`. Requires Python 3.14+ — uv downloads it for you if it isn't installed (`.python-version` pins 3.14).

   > The project standardizes on `uv` + `uv.lock`; there is no committed `requirements.txt`. If your tooling needs one (e.g. plain `pip`), generate it yourself from the lock file:
   >
   > ```sh
   > uv export --no-hashes --no-dev --output-file requirements.txt
   > ```

3. **Set up your secret sauce:**

   Copy `.env.example` to `.env` and fill it in (it documents every setting):

   ```
   GITHUB_USER=your-github-username
   GITHUB_TOKEN=your-github-token
   # Only needed for `sync` - `archive` works with the GitHub token alone
   GITLAB_USER=your-gitlab-username
   GITLAB_TOKEN=your-gitlab-token
   # Optional. Push into a group instead of your own namespace.
   # GITLAB_GROUP=your-gitlab-group
   # Optional. 'public' makes all mirrored repos public on GitLab, 'private' makes
   # them all private, 'auto' matches each repo's visibility on GitHub.
   REPO_VISIBILITY=auto
   # Optional. Percentage of CPU cores to keep busy, 1-100.
   CPU_LOAD_PERCENT=70
   # Optional. Pin how many repos are synced in parallel. 0 derives it from CPU_LOAD_PERCENT.
   MAX_WORKERS=0
   # Optional. Where the local mirrors live. Leave commented out for Docker.
   # BACKUP_DIR=./repos-backup
   # Optional. Where the zip archives are written. Leave commented out for Docker.
   # ARCHIVE_DIR=./archives
   ```

   - Get a GitHub token with repo read access (don’t share it with strangers). 🤫
   - Get a GitLab token with `api` and `write_repository` scopes (superpowers required). 🦸‍♀️ Only if you use `sync` — if all you want is zip backups, skip GitLab entirely.
   - Keep comments on their own line in `.env`: `docker run --env-file` reads a trailing `# ...` as part of the value.
   - `REPO_VISIBILITY` lets you control the visibility of your mirrored repos: set it to `public`, `private`, or `auto` to match the original.
   - `CPU_LOAD_PERCENT` and `MAX_WORKERS` decide how aggressively repos are synced in parallel. See [Going faster](#going-faster-) below.

## Usage 🎩

Run the script and watch the magic happen:

```sh
uv run python main.py
```

Your GitHub repositories will be mirrored to GitLab. The terminal shows a live dashboard — progress, what each worker is doing right now (cloning, pushing...), and the last few repos finished — and when it's done, a summary that names every repo that failed and **why** (e.g. `push: remote: GitLab: You are not allowed to force push code to a protected branch`). Every detail, including git's own output, is in the log file. Grab some popcorn 🍿 and enjoy the show!

That's shorthand for the `sync` command — GitSync also knows how to bundle your backup into a zip:

```sh
uv run python main.py sync      # mirror GitHub -> GitLab (same as no arguments)
uv run python main.py archive   # fetch the latest from GitHub, then zip it (no GitLab needed)
uv run python main.py --help    # every flag, straight from the horse's mouth
```

(`uv run python -m gitsync ...` does exactly the same thing, if you prefer modules over scripts.)

### Going faster 🏎️

Repos are synced in parallel. By default GitSync targets **70% of your CPU cores** (so 7 repos at a time on a 10-core machine), and **never fewer than 4** — syncing mostly waits on GitHub and GitLab rather than the CPU, so even a 1-CPU cloud VM handles 4 at once:

```sh
uv run python main.py sync --cpu-load 50   # gentler: half the cores
uv run python main.py sync --workers 12    # exact number of repos in flight
```

The same knobs live in `.env` as `CPU_LOAD_PERCENT` and `MAX_WORKERS`; the command line wins over both. Cloning is mostly network and disk work, so feel free to push the worker count above your core count if your connection can take it.

### Zipping your backup 🗜️

Roll every repo into a single zip file for cold storage (an external drive, cloud storage, wherever you like being redundant). `archive` first brings the local mirrors up to date from GitHub — cloning anything new, fetching everything else — so the zip is as current as your account. It only needs the GitHub token; GitLab is never touched.

```sh
uv run python main.py archive                        # everything, full history
uv run python main.py archive --mode code            # just the code, small
uv run python main.py archive --compression 9        # squeeze harder (0-9, default 6)
uv run python main.py archive -o ~/backups/gh.zip    # pick your own filename
uv run python main.py archive --no-fetch             # zip what's already on disk (offline)
```

If the fetch can't reach GitHub, or a repo fails to fetch or to archive, the zip is still written from what's on disk — but the command exits `1` and names what's missing, so a scheduled run never passes a stale or partial zip off as a good one.

Two modes, pick your poison:

| Mode             | What lands in the zip                                                                                                                                                                                                         | Good for                                                         |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `full` (default) | The complete git mirror of every repo: all branches, tags and history, plus anything you committed — vendored `node_modules`, `site-packages` and friends included. Each folder is restorable with `git clone repo-name.git`. | A real backup you can resurrect from.                            |
| `code`           | A snapshot of the latest commit on each repo's **default branch** (other branches are not included), with dependency and build folders (`node_modules`, `.venv`, `site-packages`, `dist`, `build`, `target`, caches, …) and `*.pyc`/`*.pyo`/`*.class` files stripped out. | Keeping the file size small when you only care about the source. |

Add `--exclude '<glob>'` (repeatable) to drop more paths in `code` mode, and `--backup-dir` if your mirrors live somewhere other than `./repos-backup`. Archives default to `./archives/gitsync-<mode>-<timestamp>.zip`. Repos with no commits yet are skipped with a warning.

The list of folders stripped in `code` mode is configurable: set `CODE_ARCHIVE_EXCLUDE_DIRS` in `.env` to your own comma-separated list to replace the built-in one (shown, commented out, in `.env.example`) — for example, drop `dist` from the list if you want it kept. Leave it unset and the built-in list is used. Names are matched against **folders at any depth**, so `src/build/` is stripped just like `build/` at the root — take a name out of the list if one of your source folders happens to share it. Files are never matched by these names (a script called `build` is kept); the `*.pyc`/`*.pyo`/`*.class` rule always applies.

## Docker 🐳

Prefer not to install anything? The whole thing runs from a container, configured entirely through environment variables — Alpine-based, ~120 MB, no root.

**Easiest: let the setup script do it.** Like `build.sh`, but for Docker — it asks for your tokens (masked as you paste, with links that tick the right boxes), what to run and how often, then starts the container:

```sh
bash docker-setup.sh
```

- **Pick the version** from a list of everything published (newest first, with each release's date) — `latest` by default — or type any tag, or build from this folder. Choices are arrow-key menus: ↑/↓ and Enter.
- **On a schedule in the background** (`docker run -d`, restarts after a reboot) or **just once, now**.
- **Your `.env` values are the defaults.** If a `.env` exists, every question shows what's in it — press Enter to keep it, or type something new (tokens are shown masked; `-` stops using GitLab). Or start fresh instead.
- Your schedule is checked before anything starts, and read in your machine's timezone.
- Backups go to a **`./gitsync-data` folder** you can open (zips in `archives/`, one log file per run in `logs/`) — or a Docker volume if you prefer.
- It ends with the commands you'll want: following the logs live, checking how the last run went, finding the zips, stopping and updating.
- Run it again any time to change settings or pick up a newer image; it replaces the container and keeps your backups.
- **Your choice where the settings are kept.** By default they're saved to `.env` (readable only by you), so running the script again keeps your tokens. Answer **N** to *Save to .env?* and nothing is written: each setting is handed to Docker as `-e NAME` with no value, so the tokens never appear on a command line — you just paste them again next time. Either way Docker itself keeps the settings in the container (`docker inspect gitsync` shows them) — it needs them to restart the container after a reboot — so anyone who can run `docker` on that machine can read them.

Or do it by hand:

```sh
docker build -t gitsync .

# One backup, then exit
docker run --rm --env-file .env -v gitsync-data:/data gitsync

# Or fetch the latest and zip it (GitHub token only)
docker run --rm --env-file .env -v gitsync-data:/data gitsync archive --mode code
```

No `.env` file? Pass the settings straight in with `-e` — swap each value for your own:

```sh
# One sync to GitLab, then exit
docker run --rm \
  -e GITHUB_USER="your-github-username" \
  -e GITHUB_TOKEN="your-github-token" \
  -e GITLAB_USER="your-gitlab-username" \
  -e GITLAB_TOKEN="your-gitlab-token" \
  -e REPO_VISIBILITY="auto" \
  -v gitsync-data:/data \
  gitsync

# A fresh zip - GitHub only, no GitLab settings needed
docker run --rm \
  -e GITHUB_USER="your-github-username" \
  -e GITHUB_TOKEN="your-github-token" \
  -v gitsync-data:/data \
  gitsync archive --mode code
```

Add `-e GITLAB_GROUP="your-gitlab-group"` to push into a group, or any other variable from `.env.example` the same way. Tokens typed on the command line end up in your shell history — `--env-file` avoids that.

### Keeping it running ⏰

Set `CRON_SCHEDULE` and the container stays up and syncs on that schedule:

```sh
docker run -d --name gitsync --restart unless-stopped \
  --env-file .env \
  -e CRON_SCHEDULE="0 2 * * *" \
  -e TZ="Asia/Kolkata" \
  -v gitsync-data:/data \
  gitsync
```

With `docker compose` that whole command is `docker compose up -d` — see `docker-compose.yml`.

`docker-compose.yml` holds no secrets itself — tokens and settings all come from `.env` via `env_file`, never written into the compose file. If you don't want it around, it's optional and safe to delete; the plain `docker run` commands above work just as well without it.

A few things worth knowing:

- **Restarts are free.** Everything that matters — mirrors, archives, logs — lives under `/data`. Keep that volume and a restart is a `git fetch`, not a fresh clone of your whole account. `RUN_ON_START=true` (the default) syncs immediately on start rather than waiting for the next slot, so a reboot at 3 AM doesn't cost you a day.
- **`docker stop` is clean.** SIGTERM is handled: in-flight clones are allowed to finish, a half-written zip is deleted, and the container exits 130 instead of being killed after the usual 10-second grace period.
- **Runs never overlap.** Only one sync or archive works on a backup folder at a time — every run takes a lock (`.gitsync.lock` in the backup folder) first. If a run is slow (big first clone, slow network) and the next slot comes round, that slot is skipped and the log says so: `Skipped 2 scheduled run(s) of sync at ... 14:00, ... 15:00 - the previous run (started 13:00) was still going`. A one-off started while a run is going (`docker compose run --rm gitsync sync`, or `main.py sync` by hand) does not start either: it names the run that holds the lock and exits `75`. The lock is released the moment a run ends — even if it crashed or was killed — so it can never get stuck.
- **One bad run doesn't stop the schedule.** If a scheduled run fails — GitHub or GitLab down, a network blip — the failure is logged and the next slot tries again; the container keeps running. Mistakes that would fail every time are caught once, at startup, instead: a bad `CRON_SCHEDULE` or a typo in `CRON_COMMAND` (e.g. `archive --mode everything`) stops the container straight away with the reason in `docker logs`.
- **`docker logs` shows everything.** Without a terminal the live dashboard is skipped and every log line goes to stdout instead — still with the tokens masked. Each run ends with the same summary you'd see in a terminal, one line per failed repo with the step and the reason (`Bussie - push: remote: GitLab: ...`), so `docker logs gitsync | grep -A20 finished` shows how the last run went.
- **The schedule is kept by GitSync itself**, not by cron inside the container. busybox crond won't start a job unless it is root, and this image runs as uid 1000; `main.py schedule --cron '...'` does the same job as an ordinary user. That command works outside Docker too, if you'd rather use a systemd unit than a crontab.

| Variable                                  | Default       | What it does                                                          |
| ----------------------------------------- | ------------- | --------------------------------------------------------------------- |
| `CRON_SCHEDULE`                           | _(unset)_     | Cron expression, or `@daily`/`@hourly`. At most hourly — a more frequent one (e.g. `*/15 * * * *`) is refused at startup. Unset means one sync and exit |
| `CRON_COMMAND`                            | `sync`        | What each slot runs — `sync` (to GitLab) or e.g. `archive --mode code` (a fresh zip each slot, no GitLab needed) |
| `RUN_ON_START`                            | `true`        | Run `CRON_COMMAND` as soon as the container starts, instead of waiting for the first slot |
| `TZ`                                      | `UTC`         | Timezone the schedule is read in                                      |
| `GITLAB_ALLOW_FORCE_PUSH` | `false` | `true` lets GitSync allow force pushes on a project's protected branches when GitLab refuses a push for that reason, then retry — see [Troubleshooting](#troubleshooting-) |
| `INCLUDE_FORKS` | `true` | `false` leaves your forks out of sync and archive |
| `LOG_RETENTION_DAYS` | `30` | Days to keep log files (one per run). `0` keeps them all |
| `BACKUP_DIR` / `ARCHIVE_DIR` / `LOGS_DIR` | under `/data` | Move them if your volume layout differs. Leave them out of `.env` otherwise — a local path like `./repos-backup` there would put the backup outside the volume |

Bind-mounting a host folder instead of a named volume? It has to be writable by uid 1000 (`chown -R 1000:1000 ./data`), or start the container with `--user "$(id -u):$(id -g)"`. The entrypoint checks this at startup and says so plainly rather than failing halfway through a clone.

### Published images 📦

Images go to the GitHub Container Registry, and only when you ask for one: **Actions → Publish image → Run workflow**, then give it a version. It builds for `amd64` and `arm64`, smoke-tests the image _before_ anything is pushed, and publishes three tags:

| Tag            | Moves?                   | Use it for                                          |
| -------------- | ------------------------ | --------------------------------------------------- |
| `:2.0.0`       | no                       | pinning to a release                                |
| `:sha-1a2b3c4` | never                    | tracing a running container back to an exact commit |
| `:latest`      | only if you tick the box | "just give me the newest"                           |

```sh
docker pull ghcr.io/scienmanas/gitsync:latest
docker run --rm --env-file .env -v gitsync-data:/data ghcr.io/scienmanas/gitsync:latest
```

The package page shows the image's description — what GitSync does, in a few lines. It's set in two places that tests keep identical: the `org.opencontainers.image.description` label in the `Dockerfile` (what `docker inspect` shows) and an annotation on the image index in the publish workflow (what GHCR shows — for a multi-arch image it reads only that). Change the text in both, at most 512 characters.

Locally, the version is a build argument — it ends up in the image labels and in `$GITSYNC_VERSION`:

```sh
docker build --build-arg VERSION=2.0.0 -t gitsync:2.0.0 .
docker inspect gitsync:2.0.0 --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
```

Two things worth knowing: the everyday CI never builds the image (the test suite reads the Dockerfile and entrypoint instead, so a build on every push and every Dependabot PR isn't minutes well spent), and the workflow refuses to run from any branch but `main`. GHCR packages also start out private — after the first publish, flip it to public in the package settings if you want people to pull without logging in.

## Notes 📝

- Works on macOS and Linux. Windows users, you’re on your own (for now). 🐧🍏
- Your commit history is preserved, so you can relive your coding journey. GitLab’s activity graph may look different, but your code is safe! 🧑‍💻
- The script can take a long time to run, depending on the number of repositories and their sizes. Patience is a virtue! ⏳ You can take your pet dog for a walk while it runs 🐶. Syncing several repos at once cuts that down a lot, but a first run over a big account is still a coffee-and-a-walk affair.
- Your tokens never reach the terminal or the log files: clone/push URLs and anything else that looks like a credential are masked as `***` before being written anywhere. 🔒
- **Forks are backed up too**, like any other repo — with their whole history, so a fork of a big project can be the slowest thing in your backup. Set `INCLUDE_FORKS=false` to leave them out; the log lists the ones skipped. Turning it on or off never renames your other repos.
- **GitSync backs up the repos your account owns.** Organization repos — even of an organization you created, since those belong to the organization — and repos you only collaborate on are not included yet; they are planned for a later release. 🏷️
- You can deploy it on a server and run it in the background, so you can continue your work while it does its magic. 🖥️✨

## Deploy and Automate (Local PC or Server) 🚀

**Cloud**: (I suggest using a cron-job service instead of getting an EC2 or compute instance.) Or run the [Docker image](#keeping-it-running-) with `CRON_SCHEDULE`.

**Local**: let cron run GitSync for you:

1. Set up once (skip if you already did the [Quick start](#quick-start-)):

   ```sh
   bash build.sh
   ```

2. Schedule it:

   ```sh
   bash deploy.sh
   ```

   - It asks **what to run** — `sync` to GitLab, or a zip with `archive --mode full` / `--mode code` (the default when no GitLab token is set, since `sync` would fail without one).
   - Then **how often**, as a cron expression: `0 2 * * *` is every day at 02:00, `0 * * * *` every hour. More often than hourly is refused — a run walks your whole account.
   - Running it again **replaces** the GitSync job instead of adding a second one, and your other cron jobs are left alone.
   - The job runs from the project folder, so backups land in its `repos-backup/` and `archives/` — the same place as your manual runs.

3. **Managing the cron job:**
   - See it: `crontab -l`
   - Change the schedule or the command: run `bash deploy.sh` again
   - Remove it: `crontab -e` and delete the line containing `main.py` (avoid `crontab -r` — it deletes *all* your cron jobs, not just GitSync's)

4. **Logs:**
   - All output and errors from each run are written to `shell_logs.txt` in the project root. Each run starts with a line like `Cron job triggered at ...` so you can see exactly when the job was executed.
   - Detailed logs for each backup session are also saved in the `Logs/` folder, with timestamped filenames (e.g., `logs_YYYYMMDD_HHMMSS.txt`). Every line is prefixed with the repo it belongs to, which matters now that several repos are handled at once.
   - Every run gets its own file — scheduled runs too, so a container running for months never grows one endless log. Files older than `LOG_RETENTION_DAYS` (default 30) are deleted automatically; set it to `0` to keep them all. Only `logs_*.txt` files are ever removed.
   - Credentials are redacted from every log line, so these files are safe to paste into an issue. Log files written by older versions may still contain tokens — check before sharing them, and rotate the token if one leaked.
   - Check these files for progress, errors, and troubleshooting.

### Cron, or GitSync's own scheduler? 🤔

There are two ways to run GitSync on a schedule. Pick one — you don't need both.

| | `bash deploy.sh` (your system's cron) | `main.py schedule --cron '...'` (GitSync's own scheduler) |
| --- | --- | --- |
| How it works | cron starts GitSync at each slot; the run does its job and exits | one GitSync process keeps running and starts a run at each slot |
| Between runs | nothing is running | a sleeping Python process |
| After a reboot | carries on by itself — cron comes back with the system | stops, unless something restarts it (Docker's `--restart`, a systemd service) |
| Best for | **your own machine** — a laptop, a desktop, a home server | **Docker** — it's what the image uses, since cron can't run in an unprivileged container |

Both follow the same rules: a bad schedule or one more often than hourly is refused, only one run works on a backup folder at a time (an overlapping run is skipped with exit `75`), and every run writes its own log file.

**Runs missed while the machine is off or asleep are not caught up.** If your laptop is closed at 02:00, that night's backup is skipped and the next one happens at the next slot. In Docker, `RUN_ON_START=true` (the default) runs one straight away whenever the container starts; with cron, run `uv run python main.py` by hand if you want to catch up.

## Troubleshooting 🛠

**`push: remote: GitLab: You are not allowed to force push code to a protected branch`** — the repo's history was rewritten on GitHub (an amend, a rebase, a force push), and GitLab protects every project's main branch against exactly that. Either set `GITLAB_ALLOW_FORCE_PUSH=true` in `.env` (`build.sh` asks about it) — GitSync then turns on *allowed to force push* for that project's protected branches the moment a push is refused, and pushes again; projects that never need it are left alone — or do it by hand: in GitLab, open the project → **Settings → Repository → Protected branches**, and allow force push on the branch (or unprotect it).

**`warning: Ignoring existing virtual environment linked to non-existent Python interpreter`** — harmless, nothing to fix. It means the `.venv` folder was made with a Python that isn't on this machine any more: the project folder was copied from another computer, moved, or the Python it used was upgraded or removed. uv deletes that `.venv` and builds a fresh one with the Python you have now — the lines after the warning (`Creating virtual environment at: .venv`, `Installed ... packages`) show it worked. Your `.env`, mirrors and archives are not touched. `bash build.sh` spots this before uv does and rebuilds quietly, so you'll only see the warning when running `uv sync` yourself.


If you run into issues, check the logs in `shell_logs.txt` and the timestamped files in the `Logs/` folder — they’re like breadcrumbs leading you to the solution. 🕵️‍♀️ In Docker, `docker logs gitsync` shows the same lines. If you need help, feel free to open an issue on GitHub.

## Screenshots

### 🦊 GitLab

<p align="center">
  <img src="https://github.com/user-attachments/assets/ef0333cc-63ac-4303-8da0-5eacdf6875e6" 
       alt="GitLab App Creation" 
       width="90%" />
</p>

### 🐙 GitHub

<p align="center">
  <img src="https://github.com/user-attachments/assets/58b2c89b-f07e-4a4e-9d3b-57dea50af467" 
       alt="GitHub App Creation" 
       width="70%" />
</p>

## Project structure 🗂️

`main.py` is a thin entry point (so `python main.py` and the cron job keep working); the code lives in the `gitsync/` package:

```
main.py                 # entry point - `python main.py` == `python -m gitsync`
gitsync/
├── __main__.py         # makes `python -m gitsync` work
├── config.py           # .env / environment settings and the credential check
├── redaction.py        # masks tokens before anything is logged or printed
├── logging_setup.py    # log file, live-UI buffer, per-repo log context
├── process.py          # runs git and streams its output into the log
├── github_api.py       # GitHub API calls
├── gitlab_api.py       # GitLab API calls
├── ui.py               # shared rich widgets (progress bar, live dashboard)
├── sync.py             # the sync command (parallel mirror + push)
├── archive.py          # the archive command (zip the local mirrors)
├── schedule.py         # the schedule command (cron expressions, kept in-process)
├── runlock.py          # one sync/archive at a time per backup folder (.gitsync.lock)
└── cli.py              # argument parsing and dispatch
tests/                  # pytest suite, one file per module
Dockerfile              # Alpine image, unprivileged, git + the venv
docker-entrypoint.sh    # picks a mode from the environment and starts it
docker-compose.yml      # scheduled container with a volume, ready to `up -d`
docker-setup.sh         # asks a few questions, writes .env and starts the container
```

Nothing runs at import time beyond reading the environment, so you can poke at any module in a REPL without kicking off a backup.

### Running the tests 🧪

Needs uv 0.12.22 or newer (`uv self update` if yours is older) — `uv sync` installs pytest along with everything else.

```sh
uv run pytest                            # the whole suite
uv run pytest tests/test_schedule.py     # one file
uv run pytest -k clash                   # only tests whose name contains "clash"
uv run pytest -x                         # stop at the first failure
uv run pytest -q -o addopts=""           # short: dots and the total, no log output
```

**`ERROR` and `Traceback` lines in the output are not failures.** By default every log line GitSync writes is printed live, and many tests break things on purpose — a clone that cannot connect, a push that is rejected, a mirror with its objects deleted — to check that GitSync reports it and recovers. What counts is pytest's verdict after each test name: `PASSED`, or `FAILED` for a real failure (listed again in a `FAILURES` section at the end). The total is the last line, e.g. `300 passed in 4s`.

No tokens, no network, no Docker daemon: the GitHub/GitLab calls are stubbed and the tests that need real repositories build throwaway ones with `git` in a temp folder. They cover:

- **Sync** — parallel worker sizing, the success/failure tally, Ctrl-C, pushing to your namespace or a group, GitLab project creation and visibility, repos that share a name, and replacing a mirror that is damaged or holds a different repo without ever losing the old one.
- **The APIs** — timeouts on every call, a GitLab error never mistaken for "project not found", a failed GitHub listing failing the run instead of passing as "nothing to sync".
- **Archive** — both modes (including extracting a `full` archive and cloning a repo back out of it), dependency stripping, a failed repo failing the run, the half-written zip removed on Ctrl-C, and fetching from GitHub first.
- **Scheduling** — cron parsing (names, ranges, steps, impossible dates), the at-most-hourly limit, the loop itself on a fake clock, stopping cleanly, carrying on after a failed run, logging the slots a long run overlapped, and one run at a time per backup folder (a second one is skipped; the lock frees itself even after a crash).
- **Logging and safety** — credential masking everywhere, one log file per run and old ones aged out, and git giving up on a stalled transfer.
- **Packaging** — the Dockerfile, entrypoint and `.env.example` agreeing on paths and settings.

CI runs them on every push and pull request (it does not build the image — see [Published images](#published-images-)), and a separate workflow runs `pip-audit` on the dependencies.

## Coming in 3.0.0 🗺️

**Organization repositories.** 2.0.0 backs up the repos your own account owns. Repos that belong to an organization — including one you created yourself, since GitHub treats those as the organization's, not yours — are not included yet. The plan:

- **Pick the organizations to back up** with a new setting, e.g. `GITHUB_ORGS=my-org,other-org`. Only the organizations you name are backed up, never every organization you happen to be a member of.
- **Every repo of each named organization** is mirrored, zipped and pushed to GitLab, the same way your own repos are.
- **Repos that share a name stay apart.** If you own a `dotfiles` and `my-org` has one too, yours keeps `dotfiles` and the organization's is backed up as `my-org-dotfiles` — as its mirror folder, its folder in the zip and its GitLab project. (This handling is already in 2.0.0, waiting for the repos that need it.)
- **Token guidance.** A classic token with the `repo` scope can read your repos and your organizations' repos together; a fine-grained token belongs to a single owner — you *or* one organization — so it cannot cover both.

**Checking your usernames against your tokens.** A typo in `GITLAB_USER` (or `GITHUB_USER`) currently shows up as every repo failing — GitSync looks for projects in an account that doesn't exist. 3.0.0 will ask GitHub and GitLab who each token belongs to before anything runs:

- **Every run starts with the check**, and stops straight away with a clear message if the name doesn't match — e.g. *`GITLAB_USER` is "sciemanas", but your GitLab token belongs to "scienmanas"* — instead of failing repo by repo.
- **`build.sh` checks as soon as you paste a token**, and fills in the username for you, so there's nothing to mistype.

## Contributing 🤝

Contributions are welcome — bug reports, ideas, docs and code. [CONTRIBUTING.md](CONTRIBUTING.md) has the setup, how to add tests and the pull request checklist; the short version:

```sh
git clone https://github.com/scienmanas/GitSync.git
cd GitSync
uv sync              # creates .venv with Python 3.14, the dependencies and pytest
uv run pytest        # everything should pass before you start
```

- Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
- Found a security problem? Report it privately — see [SECURITY.md](SECURITY.md), not a public issue.
- What changed in each release: [CHANGELOG.md](CHANGELOG.md).
- I only have macOS and Linux, so it is tested on those platforms and works. I’d love it if someone could test it on a Windows machine. If any errors come up, please open an issue detailing them. I will fix them when I can. 🐧🍏
- If some issues are opened and not assigned (except to me), just jump to the discussion section and ping me there if you are interested in fixing them. I will assign you the issue, and you can work on it. 🛠️

## License 📄

MIT License. Use, share, and enjoy! 😄
