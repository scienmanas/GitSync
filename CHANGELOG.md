# Changelog

All notable changes to GitSync are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased][Unreleased]
### Planned for 3.0.0

- **Username check.** Before anything runs, ask GitHub and GitLab who each token belongs to and stop with a clear message if `GITHUB_USER` / `GITLAB_USER` doesn't match — a typo currently makes every repo fail. `build.sh` will check right after the token is pasted and fill the username in itself.
- **Organization repositories.** Back up the repos of the GitHub organizations you name (e.g. `GITHUB_ORGS=my-org,other-org`), alongside your own. See [Coming in 3.0.0](README.md#coming-in-300-).

### Added - 2026-10-03

- **`docker-setup.sh`** — `build.sh` for Docker: lets you pick the image version from the ones published (newest first, with release dates, `latest` by default) or type any tag, offers your existing `.env` values as the defaults (Enter keeps each), asks for your tokens (masked), what to run and how often — all choices as arrow-key menus — then starts GitSync in the background on a schedule (`docker run -d`) or once in the foreground. Backups go to a visible `./gitsync-data` folder by default, the schedule is checked before anything starts and read in your machine's timezone, and it ends with how to follow the logs, read earlier runs and find the zips. Settings are saved to `.env` for next time, or — if you'd rather not keep a file — passed straight to Docker as `-e NAME` without the value, so tokens never appear on a command line.

## [2.0.0][2.0.0] - 2026-10-03

A rewrite that turns the single script into a package, adds zip archives, Docker and a built-in scheduler, and makes GitSync safe to leave running unattended.

### Upgrading from 1.0.0

- **Python 3.14 or newer** and **uv 0.12.22 or newer** are required (was Python 3.12+ and any uv). Run `uv self update`, then `uv sync` — uv downloads Python 3.14 if needed.
- **`python main.py` still works** exactly as before, so existing `deploy.sh` cron jobs keep running. `python -m gitsync` works too.
- **If you set up a cron job with 1.0.0's `deploy.sh`, run `bash deploy.sh` once.** That job kept its backups in your home folder (`~/repos-backup`, `~/archives`) instead of the project's — see *Fixed*. The new script replaces the old job; afterwards you can move or delete those two folders by hand.
- **Running `build.sh` again no longer wipes your `.env`** — it asks first, and keeps it by default.
- **Re-check your `.env` against `.env.example`.** Comments now go on their own line (`docker run --env-file` reads a trailing `# ...` as part of the value), and `BACKUP_DIR` / `ARCHIVE_DIR` should stay unset if you use Docker.
- **GitLab is now optional.** Only `sync` needs `GITLAB_USER` and `GITLAB_TOKEN`; `archive` works with the GitHub token alone.
- **Exit codes now mean something**: `0` success, `1` a repo or the run failed, `2` bad arguments or cron expression, `75` skipped because another run was going, `130` interrupted.
- Your existing mirrors in `repos-backup/` are reused — no re-clone.

### Added

- **`archive` command**: fetches the latest from GitHub, then bundles every repo into one zip. `--mode full` keeps the complete git mirror (restorable with `git clone`); `--mode code` keeps only the latest commit's files with dependency and build folders stripped. Options for compression, output path, extra excludes (`--exclude`) and working offline (`--no-fetch`). The stripped folder list is configurable with `CODE_ARCHIVE_EXCLUDE_DIRS`.
- **`schedule` command**: stays running and repeats `sync` or `archive` on a cron expression (`0 2 * * *`, `@daily`, `0 9 * * MON-FRI`). Kept by GitSync itself, so it works in an unprivileged container. At most hourly; impossible dates (31 February) are refused.
- **Docker image** (Alpine, ~120 MB, runs as uid 1000) with an entrypoint that runs once or on `CRON_SCHEDULE`, a `docker-compose.yml`, and a manually triggered workflow that publishes multi-arch images to GHCR — with a description on the package page.
- **Parallel sync**: several repos at once, sized by `CPU_LOAD_PERCENT` (default 70%, and never fewer than 4 — syncing waits on the network, not the CPU) or pinned with `MAX_WORKERS` / `--workers`.
- **Live dashboard and summary**: the terminal shows progress, what each worker is doing (cloning, checking GitLab, pushing) and the last repos finished; at the end, a summary names every failed repo with the step and git's own reason. It never grows past the screen, so it doesn't flicker and leaves nothing half-drawn behind. Raw git output goes to the log file only.
- **Credential masking**: tokens are redacted from every log line, the terminal and crash tracebacks.
- **One run at a time** per backup folder (`.gitsync.lock`). A second run is skipped with exit `75`; a scheduled slot that a long run overlaps is skipped and logged. The lock frees itself if a run crashes.
- **`GITLAB_ALLOW_FORCE_PUSH`** setting (off by default; `build.sh` asks): when GitLab refuses a push because the branch is protected — the repo's history was rewritten on GitHub — GitSync allows force push on that project's protected branches and pushes again. Without it, the summary says which setting fixes it.
- **`INCLUDE_FORKS`** setting to leave your forks out of sync and archive.
- **`LOG_RETENTION_DAYS`** (default 30): every run writes its own log file and old ones are deleted.
- **Timeouts** on every GitHub/GitLab API call, and git gives up on a transfer that has stalled.
- **Test suite** (300+ tests, no network or tokens needed), run in CI on every push and pull request.
- **Quick start** at the top of the README: clone, `bash build.sh`, run, `bash deploy.sh`.
- Project files: `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue and pull request templates, and package metadata in `pyproject.toml`.

### Changed

- The code moved from one `main.py` into the `gitsync/` package; `main.py` is now a thin entry point.
- A repo is only counted as synced when cloning, the GitLab project check and the push all succeeded; any failure makes the run exit `1`.
- A scheduled run that fails (GitHub or GitLab down) is retried at the next slot instead of stopping the schedule.
- `docker stop` / Ctrl-C finish in-flight repos, delete a half-written zip and exit `130`.
- Repos that would share a folder and GitLab project are kept apart (`<owner>-<name>`), ready for organization repos in 3.0.0.
- **`build.sh`** asks only what is needed — your GitHub user and token, and GitLab if you want to push there — with everything else behind an *Advanced settings?* question and left on GitSync's defaults when blank. It never overwrites an existing `.env` without asking, hides tokens as you type them, makes `.env` readable only by you, checks that uv is new enough before starting, and no longer deletes `.venv` on every run.
- **`deploy.sh`** asks what to run — `sync`, or a zip with `archive` (the default when no GitLab token is set, since `sync` would fail every time) — checks the schedule with GitSync's own rules (no more often than hourly), and replaces an earlier GitSync cron job instead of adding a second one. Your other cron jobs are left alone.

### Fixed

- With `GITLAB_GROUP` set, every run after the first failed every repo: projects were looked up by the group's numeric id instead of its path.
- A GitLab error (bad token, rate limit, outage) was treated as "project not found", followed by a failed attempt to create it.
- A bad GitHub token or a GitHub outage looked like an empty account and the run exited `0` ("nothing to sync").
- The repo-list request had a stray `&&` and an `affiliation` value GitHub does not recognise; it now asks for exactly the repos you own.
- A stalled network connection could hang a run forever: neither the API calls nor git had a timeout.
- **A failed fetch could wipe the local backup.** The mirror was deleted before recloning, so if GitHub or the network was down the reclone failed too and nothing was left. The fresh clone is now made next to the old mirror and swapped in only once it has worked.
- The placeholder `GITLAB_GROUP=your-gitlab-group` from `.env.example` was looked up as a real group.
- **Cron jobs set up by `deploy.sh` kept their backups in your home folder.** Cron starts jobs in `~`, and the job never changed into the project folder, so mirrors and zips went to `~/repos-backup` and `~/archives` — separate from manual runs, with a full re-clone on the first cron run. The job now starts in the project folder.
- `deploy.sh` suggested running every 15 minutes; it now refuses anything more often than hourly.
- The README told you to remove the cron job with `crontab -r`, which deletes **all** your cron jobs, not just GitSync's.

## [1.0.0][1.0.0] - 2026-09-29

First release.

### Added

- Mirrors every GitHub repository to GitLab with full commit history.
- Personal and group namespaces on GitLab (`GITLAB_GROUP`).
- Visibility for mirrored repos via `REPO_VISIBILITY`: `public`, `private`, or `auto` (matches each repo's GitHub visibility).
- Rich terminal UI with live progress and logging.
- Timestamped session logs in `Logs/`.
- `build.sh` sets up the environment; `deploy.sh` schedules a cron job, with output logged to `shell_logs.txt`.
- Dependencies managed with `uv` and a pinned `uv.lock`.
- Works on macOS and Linux (Python 3.12+).

[Unreleased]: https://github.com/scienmanas/GitSync/compare/v1.0.0...HEAD
[2.0.0]: https://github.com/scienmanas/GitSync/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/scienmanas/GitSync/releases/tag/v1.0.0
