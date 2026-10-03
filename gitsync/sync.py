"""The sync command: mirror every GitHub repo locally, then push it to GitLab.

Repos are handled in parallel by a thread pool sized from CPU_LOAD_PERCENT.
"""

import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.live import Live
from rich.markup import escape

from . import config, github_api, gitlab_api
from .logging_setup import (console, log_file_path, set_current_repo)
from .process import last_git_error, run
from .ui import ActiveRepos, SyncDashboard, make_progress, print_summary

logger = logging.getLogger(__name__)

# Mirror a github repo to local


def mirror_repo(repo_name, github_url, local_path) -> bool:
    # Build URL to clone from
    if github_url.startswith("https://"):
        auth_clone_url = github_url.replace(
            "https://", f"https://{config.GITHUB_USER}:{config.GITHUB_TOKEN}@", 1)
    else:
        auth_clone_url = github_url

    # Create a local copy of the github repo
    if not os.path.exists(local_path):
        logger.info("Cloning (mirror) %s ...", repo_name)
        try:
            run(["git", "clone", "--mirror", auth_clone_url, local_path])
        except Exception as e:
            logger.exception("Failed to clone %s: %s", repo_name, e)
            return False
        return True

    # The folder may hold a *different* repo: a name that used to belong to
    # one repo can be handed to another (see local_names - e.g. you create your
    # own "dotfiles" next to an org's). A fetch would keep pulling from the URL
    # stored in the mirror, i.e. keep backing up the wrong repo, so replace it.
    # Both cases end in a fresh clone; they differ only in what the log says,
    # so it tells you the real reason.
    match = _mirror_matches(local_path, github_url)
    if match is None:
        logger.warning("%s cannot be read as a mirror (damaged, or no remote) - "
                       "replacing it with a fresh clone of %s", local_path, github_url)
        return _reclone(repo_name, auth_clone_url, local_path)
    if not match:
        logger.warning("%s holds a mirror of a different repo - replacing it "
                       "with a fresh clone of %s", local_path, github_url)
        return _reclone(repo_name, auth_clone_url, local_path)

    try:
        run(["git", "--git-dir", local_path, "fetch", "--all", "--prune"])
    except Exception as e:
        # Not an error yet - the reclone below may well fix it
        logger.warning("Fetch failed for %s (%s) - retrying with a fresh clone",
                       repo_name, e)
        return _reclone(repo_name, auth_clone_url, local_path)
    return True

# Does the mirror at local_path come from github_url? Compared without the
# credentials (the stored URL carries the token the clone was made with, which
# may since have changed) and ignoring case and a trailing "/".
#
# Returns True (same repo), False (a different repo) or None (the stored URL
# cannot be read: no origin remote, or a folder too damaged for git to open -
# e.g. its objects/ folder is gone). None must never be treated as "same":
# a fetch in a mirror with no remote fetches nothing and still exits 0, so the
# mirror would never update while every run reported it as fetched. The caller
# sends both False and None to _reclone, which replaces the folder with a real
# clone (keeping the old one until that has worked) - they are kept apart only
# so the log can name the real reason.


def _mirror_matches(local_path, github_url) -> bool | None:
    result = subprocess.run(
        ["git", "--git-dir", local_path, "config", "--get", "remote.origin.url"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return _normalise_url(result.stdout.strip()) == _normalise_url(github_url)


def _normalise_url(url) -> str:
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^@/]*@", r"\1", url,
                  flags=re.IGNORECASE).rstrip("/").lower()

# Replace the mirror with a fresh clone, never losing the old one on the way.
# Clone next to the old mirror and only swap it in once the clone has worked:
# a fetch usually fails because GitHub or the network is down, and then the
# reclone fails too - deleting first would turn an out-of-date backup into no
# backup at all. The ".reclone" / ".old" suffixes keep the in-between copies
# out of archive's *.git scan.


def _reclone(repo_name, auth_clone_url, local_path) -> bool:
    fresh_path = f"{local_path}.reclone"
    shutil.rmtree(fresh_path, ignore_errors=True)  # leftover of a killed run
    try:
        run(["git", "clone", "--mirror", auth_clone_url, fresh_path])
    except Exception as e2:
        shutil.rmtree(fresh_path, ignore_errors=True)
        logger.exception("Reclone failed for %s: %s - keeping the existing mirror",
                         repo_name, e2)
        return False
    # Swap with renames, deleting nothing until the new mirror is in place: the
    # old one steps aside to .old, the fresh one moves in, and only then does
    # .old go. If the second rename fails, the old mirror is renamed straight
    # back. (A rename within one folder is a single, near-instant step, unlike
    # rmtree on a big mirror.)
    old_path = f"{local_path}.old"
    shutil.rmtree(old_path, ignore_errors=True)  # leftover of a killed run
    try:
        os.rename(local_path, old_path)
        try:
            os.rename(fresh_path, local_path)
        except Exception:
            os.rename(old_path, local_path)   # put the old mirror back
            raise
    except Exception as e3:
        shutil.rmtree(fresh_path, ignore_errors=True)
        logger.exception("Could not swap in the fresh clone of %s: %s - "
                         "keeping the existing mirror", repo_name, e3)
        return False
    shutil.rmtree(old_path, ignore_errors=True)
    logger.info("Recloned %s - the fresh mirror replaced the old one", repo_name)
    return True

# Chech repo in gitlab exists or not, if not make it


def ensure_gitlab_project(group_id, repo_name, user_name, repo_visibility) -> bool:
    # Caught here rather than left to process_repo, which would skip the push
    # - and the project most likely exists already, so the push may still work
    try:
        proj = gitlab_api.get_project(
            user_name=user_name, group_id=group_id, repo_name=repo_name)
    except Exception as e:
        logger.error("Could not check GitLab project %s: %s", repo_name, e)
        return False
    if not proj:
        logger.info("Project %s not found on GitLab. Creating...", repo_name)
        try:
            created = gitlab_api.create_project(
                group_id=group_id, repo_name=repo_name, visibility=repo_visibility)
            return created is not None
        except Exception as e:
            logger.exception(
                "Could not create GitLab project %s: %s", repo_name, e)
            return False
    else:
        current_visibility = proj.get("visibility")
        if current_visibility != repo_visibility:
            logger.info("Project %s exists on GitLab with visibility '%s' but desired is '%s'. Updating...",
                        repo_name, current_visibility, repo_visibility)
            try:
                updated = gitlab_api.update_project_visibility(
                    proj["id"], repo_visibility)
                return updated is not None
            except Exception as e:
                logger.exception(
                    "Failed to update visibility for %s: %s", repo_name, e)
                return False
        else:
            logger.info("Project %s exists on GitLab with matching visibility '%s'.",
                        repo_name, current_visibility)
    return True

# Push the local mirror to gitlab


def push_to_gitlab(group_id, user_name, gitlab_token, repo_name, local_path) -> bool:
    target_namespace = config.GITLAB_GROUP if group_id else user_name
    gl_repo_url = f"{config.GITLAB_URL}/{target_namespace}/{repo_name}.git"
    if gl_repo_url.startswith("https://"):
        push_url = gl_repo_url.replace(
            "https://", f"https://oauth2:{gitlab_token}@", 1)
    else:
        push_url = gl_repo_url

    logger.info("Pushing %s -> GitLab (%s) ...", repo_name, target_namespace)
    try:
        run(["git", "--git-dir", local_path, "push", "--mirror", push_url])
    except Exception as e:
        logger.exception("Push failed for %s: %s", repo_name, e)
        return False
    return True

# Decide how many repos to work on at once
# (the floor for the CPU-based default - see resolve_worker_count)
MIN_DEFAULT_WORKERS = 4


def resolve_worker_count(total_repos, workers_override=None, cpu_load_override=None) -> int:
    explicit = workers_override if workers_override is not None else (
        config.MAX_WORKERS or None)
    if explicit is not None:
        workers = max(1, explicit)
    else:
        # git is mostly waiting on the network/disk, so this is a budget for how
        # much of the machine we are willing to keep busy, not a hard cap.
        load = cpu_load_override if cpu_load_override is not None else config.CPU_LOAD_PERCENT
        load = min(100, max(1, load))
        # At least MIN_DEFAULT_WORKERS: a small cloud VM has 1-2 CPUs, which
        # would mean one repo at a time - while syncing is mostly waiting on
        # GitHub and GitLab, not using the CPU. An explicit count above wins.
        workers = max(MIN_DEFAULT_WORKERS,
                      round((os.cpu_count() or 1) * load / 100))
    return max(1, min(workers, total_repos))

# Work out the visibility the GitLab side should end up with


def resolve_visibility(repo) -> str:
    if not config.REPO_VISIBILITY:
        return "private"
    if config.REPO_VISIBILITY == "auto":
        return "private" if repo.get("private", True) else "public"
    return config.REPO_VISIBILITY

# The name each repo is backed up under - its mirror folder (<name>.git, which
# archive zips under that name too) and its GitLab project. Usually just the
# GitHub name - and today always so: the list only holds repos your own account
# owns (affiliation=owner), and GitHub never lets one owner have two repos with
# the same name. This is here for when organization repos join the list (a
# later release): two owners can each have a "dotfiles", and with one shared
# name both would mirror into the same folder - two workers cloning
# into it at once - and push --mirror into the same GitLab project, each run
# wiping the other's branches. So when names clash:
#
#   - your own repo keeps the plain name ("dotfiles")
#   - every other one becomes "<owner>-<name>" ("my-org-dotfiles")
#
# Nothing changes for repos without a clash, and who gets the plain name never
# depends on the order GitHub lists them in. Clashes are found ignoring case:
# macOS folders and GitLab paths treat "Dotfiles" and "dotfiles" as the same.
# A single "-" because GitLab refuses two special characters in a row
# ("my-org__dotfiles"). In the unlikely event the result is itself taken, a
# "-2", "-3"... suffix keeps it unique.
#
# Returns the names in the same order as repos.


def local_names(repos, github_user) -> list[str]:
    def owner_of(repo):
        owner = (repo.get("owner") or {}).get("login")
        if not owner and "/" in repo.get("full_name", ""):
            owner = repo["full_name"].split("/", 1)[0]
        return owner or ""

    by_name = {}
    for repo in repos:
        by_name.setdefault(repo["name"].lower(), []).append(repo)

    wanted = []
    for repo in repos:
        clashes = len(by_name[repo["name"].lower()]) > 1
        owner = owner_of(repo)
        if clashes and owner.lower() != (github_user or "").lower() and owner:
            wanted.append(f"{owner}-{repo['name']}")
            logger.info("Two repos are named %r - backing up %s/%s as %r",
                        repo["name"], owner, repo["name"], wanted[-1])
        else:
            wanted.append(repo["name"])

    # Plain names claim their spot first, so a prefixed name can never push a
    # plain one aside; then anything still taken gets a numeric suffix
    names = [None] * len(repos)
    used = set()
    order = sorted(range(len(repos)),
                   key=lambda i: (wanted[i] != repos[i]["name"],
                                  repos[i].get("full_name", ""), i))
    for i in order:
        candidate, n = wanted[i], 2
        while candidate.lower() in used:
            candidate, n = f"{wanted[i]}-{n}", n + 1
        used.add(candidate.lower())
        names[i] = candidate
    return names


# One repo, end to end - runs on a worker thread. Reports back with a bool and
# never exits: SystemExit (what sys.exit raises) only ends the process from the
# main thread - raised in a worker it is captured in the future and quietly lost,
# taking the exit code with it. push=False stops after the local mirror, which
# is all an archive needs - GitLab is never touched.


def process_repo(repo, gitlab_group_id, active, push=True, backup_dir=None,
                 name=None) -> bool:
    # The name used locally and on GitLab - from local_names(), which only ever
    # differs from GitHub's name when two repos would otherwise collide
    repo_name = name or repo["name"]
    github_url = repo["clone_url"]
    repo_visibility = resolve_visibility(repo)
    local_path = os.path.join(backup_dir or config.BACKUP_DIR, f"{repo_name}.git")

    set_current_repo(repo_name)
    active.add(repo_name)
    try:
        # Each step tells the dashboard what this worker is doing; a failed
        # step keeps git's own error line (last_git_error) as the reason the
        # summary shows - e.g. GitLab refusing a force push.
        active.step(repo_name, "↓ fetching from GitHub" if os.path.exists(local_path)
                    else "↓ cloning from GitHub")
        mirrored = mirror_repo(
            repo_name=repo_name, github_url=github_url, local_path=local_path)
        mirror_error = None if mirrored else (
            last_git_error() or "could not get it from GitHub")
        if not push:
            if mirrored:
                logger.info("✅ Fetched %s", repo_name)
            else:
                logger.error("❌ Failed to fetch %s", repo_name)
            active.finish(repo_name, mirrored, "fetch", mirror_error)
            return mirrored
        # Each step logs its own failure and the next one is still attempted
        # (an out-of-date mirror is better than none), but a repo only counts as
        # synced when every step worked.
        active.step(repo_name, "◇ checking the GitLab project")
        validated = ensure_gitlab_project(
            group_id=gitlab_group_id, repo_name=repo_name,
            user_name=config.GITLAB_USER, repo_visibility=repo_visibility)
        active.step(repo_name, "↑ pushing to GitLab")
        pushed = push_to_gitlab(
            group_id=gitlab_group_id, user_name=config.GITLAB_USER,
            gitlab_token=config.GITLAB_TOKEN, repo_name=repo_name, local_path=local_path)
        push_error = None if pushed else last_git_error()
        # GitLab refused a force push to a protected branch: the repo's history
        # was rewritten on GitHub. If allowed to, turn force push on for the
        # project's protected branches and try once more - otherwise say how.
        if not pushed and push_error and "protected branch" in push_error:
            if config.GITLAB_ALLOW_FORCE_PUSH:
                active.step(repo_name, "◇ allowing force push on GitLab")
                try:
                    changed = gitlab_api.allow_force_push(
                        repo_name=repo_name, user_name=config.GITLAB_USER,
                        group_id=gitlab_group_id)
                    logger.warning("Allowed force push on %d protected branch(es) of "
                                   "%s (GITLAB_ALLOW_FORCE_PUSH=true) - pushing again",
                                   changed, repo_name)
                    active.step(repo_name, "↑ pushing to GitLab (again)")
                    pushed = push_to_gitlab(
                        group_id=gitlab_group_id, user_name=config.GITLAB_USER,
                        gitlab_token=config.GITLAB_TOKEN, repo_name=repo_name,
                        local_path=local_path)
                    push_error = None if pushed else last_git_error() or push_error
                except Exception as e:
                    logger.error("Could not allow force push on %s: %s", repo_name, e)
                    push_error = f"{push_error} - and allowing force push failed: {e}"
            else:
                push_error += (" - set GITLAB_ALLOW_FORCE_PUSH=true to let GitSync "
                               "allow it")
        if mirrored and validated and pushed:
            logger.info("✅ Synced %s", repo_name)
            active.finish(repo_name, True)
            return True
        # The first step that failed is the one to report
        if not mirrored:
            step, reason = "fetch", mirror_error
        elif not validated:
            step, reason = "GitLab project", "could not check or create it - see the log"
        else:
            step, reason = "push", push_error or "push to GitLab failed"
        logger.error("❌ Failed %s (mirror=%s, gitlab-project=%s, push=%s)",
                     repo_name, mirrored, validated, pushed)
        active.finish(repo_name, False, step, reason)
        return False
    except Exception as e:
        logger.error("❌ Failed %s: %s", repo_name, e)
        active.finish(repo_name, False, "error", str(e))
        return False
    finally:
        active.remove(repo_name)       # no-op once finish() has recorded it
        set_current_repo(None)

# Refuse to start a sync with half-configured credentials. Runs on the main
# thread before the pool exists, so raising SystemExit here does stop the run.
# The GitLab pair is only demanded when we are about to push there.


def validate_env(gitlab=True) -> None:
    missing = config.missing_credentials(gitlab=gitlab)
    if missing:
        message = ("Missing or unset configuration: " + ", ".join(missing) +
                   ". Add them to your .env file (see .env.example).")
        logger.error(message)
        console.print(f"[red]{escape(message)}[/red]")
        raise SystemExit(1)

# Mirror every GitHub repo locally, then onto GitLab. push=False is the
# fetch-only run archive does first: same parallel pool, no GitLab at all.


def run_sync(workers_override=None, cpu_load_override=None, push=True,
             backup_dir=None) -> int:
    backup_dir = backup_dir or config.BACKUP_DIR
    verb = "synced" if push else "fetched"
    started = time.monotonic()
    skipped_forks = 0

    # Check all env are present
    validate_env(gitlab=push)

    # Ensure the backup directory exists
    os.makedirs(backup_dir, exist_ok=True)

    # Get the list of repos to sync, and bail if there are none
    repos = github_api.get_repos()
    if repos is None:
        message = "Could not fetch the repo list from GitHub - see the log."
        logger.error(message)
        console.print(f"[red]{message}[/red] Logs: {log_file_path()}")
        return 1
    if not repos:
        logger.info("No repos found; exiting.")
        console.print(
            f"No repositories found - nothing to {'sync' if push else 'fetch'}.")
        return 0

    # One name per repo, unique even when two owners share a repo name.
    # Worked out over the FULL list, forks included, before any are left out
    # below - so flipping INCLUDE_FORKS never renames another repo. (Without
    # forks in the list, my-org/api would take the plain name "api" from your
    # fork me/api, and its next push --mirror would overwrite your fork's
    # existing backup on GitLab.)
    names = local_names(repos, config.GITHUB_USER)

    if not config.INCLUDE_FORKS:
        forks = [repo["full_name"] if "full_name" in repo else repo["name"]
                 for repo in repos if repo.get("fork")]
        if forks:
            kept = [(repo, name) for repo, name in zip(repos, names)
                    if not repo.get("fork")]
            repos = [repo for repo, _ in kept]
            names = [name for _, name in kept]
            logger.info("Skipping %d fork(s) (INCLUDE_FORKS=false): %s",
                        len(forks), ", ".join(forks))
            skipped_forks = len(forks)
        if not repos:
            console.print("Every repository is a fork and INCLUDE_FORKS=false - "
                          f"nothing to {'sync' if push else 'fetch'}.")
            return 0

    # Get the gitlab group ID if a group is configured, so we can create projects in it. If not, the user namespace will be used instead. This is done once on the main thread rather than
    gitlab_group_id = gitlab_api.get_group_id() if push else None

    # Decide how many repos to work on at once, and log the plan.
    workers = resolve_worker_count(
        total_repos=len(repos), workers_override=workers_override,
        cpu_load_override=cpu_load_override)
    logger.info("%s %d repos with %d parallel worker(s) on %d CPU core(s)",
                "Syncing" if push else "Fetching", len(repos), workers,
                os.cpu_count() or 1)

    # Build a progress bar (top)
    progress = make_progress()
    task = progress.add_task(
        description=f"🔄 {'Syncing' if push else 'Fetching'} {len(repos)} repos "
                    f"({workers} at a time)",
        total=len(repos))

    # What each worker is doing, what finished, and why anything failed - for
    # the dashboard and the summary at the end
    active = ActiveRepos()
    succeeded = 0
    failed = 0
    interrupted = False

    # Live UI: progress, counts, what each worker is doing, recent results -
    # see ui.SyncDashboard. Redrawing needs a cursor to move, so without a
    # terminal (docker, cron, CI) there is no dashboard: the plain log lines on
    # stdout tell the story, and the same summary is printed at the end.
    dashboard = (
        Live(SyncDashboard(progress, active, len(repos), verb), refresh_per_second=4,
             transient=True, console=console)
        if console.is_terminal else contextlib.nullcontext())

    with dashboard:
        pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="gitsync")
        try:
            # All repos get queued at once - submit() returns a Future right
            # away, and the threads (gitsync_0, gitsync_1, ...) take repos off
            # the queue `workers` at a time, grabbing the next as each finishes.
            futures = [pool.submit(process_repo, repo, gitlab_group_id, active,
                                   push, backup_dir, name)
                       for repo, name in zip(repos, names)]
            # One ordinary, synchronous loop on the main thread: it runs exactly
            # once per repo, in the order they *finish* - not list order, and it
            # can differ every run. Between runs the main thread sleeps inside
            # as_completed until a worker finishing a repo wakes it, so the bar
            # and counts update as repos finish rather than all at the end (and
            # that wait is where Ctrl-C almost always lands). Only this thread
            # touches the counters, so they need no lock.
            for future in as_completed(futures):
                if future.result():
                    succeeded += 1
                else:
                    failed += 1
                progress.advance(task, 1)
                logger.info("Repos done: %s/%s", succeeded + failed, len(repos))
        except KeyboardInterrupt:
            interrupted = True
            logger.warning(
                "Interrupted by user - waiting for in-flight repos to finish.")
            pool.shutdown(wait=True, cancel_futures=True)
        finally:
            # No clone may outlive this function: wait=True joins every worker,
            # so the caller (and the sys.exit in main.py) only sees an exit code
            # once the last repo is done. The pool's threads are non-daemon, so
            # the interpreter would wait for them at shutdown rather than kill
            # one mid-write - this makes it explicit and keeps the live UI up
            # until they have actually finished.
            pool.shutdown(wait=True)

    # The summary: in a terminal it is what stays on screen once the transient
    # dashboard is gone; in docker/cron it is the last thing in the output.
    # Every failed repo is named with the step and git's reason. The log file
    # gets the same facts.
    took = time.monotonic() - started
    logger.info("%s %d repos in %s: %d %s, %d failed%s",
                "Interrupted after" if interrupted else "Finished", len(repos),
                f"{took:.0f}s", succeeded, verb, failed,
                f", {skipped_forks} fork(s) skipped" if skipped_forks else "")
    for name, step, reason in active.failures:
        logger.error("✖ %s - %s: %s", name, step, reason)
    print_summary(active, verb, took, skipped_forks, log_file_path(),
                  interrupted=interrupted)

    # 130 is the shell convention for "stopped by Ctrl-C": 128 + SIGINT (2).
    # It lets scripts and CI tell an interrupted run from a failed one (1).
    if interrupted:
        return 130
    return 0 if failed == 0 else 1
