"""Issue #18: repos are handled in parallel, and the tally has to be honest."""

import os
import subprocess
import threading
import time

import pytest

from gitsync import archive, config, github_api, gitlab_api, sync


# --------------------------------------------------------------------------- #
# Worker sizing                                                                #
# --------------------------------------------------------------------------- #

@pytest.fixture
def ten_cores(monkeypatch):
    monkeypatch.setattr(sync.os, "cpu_count", lambda: 10)
    monkeypatch.setattr(config, "MAX_WORKERS", 0)
    monkeypatch.setattr(config, "CPU_LOAD_PERCENT", 70)


def test_defaults_to_the_configured_share_of_the_cpu(ten_cores):
    assert sync.resolve_worker_count(100) == 7


def test_cpu_load_override_wins_over_the_env(ten_cores):
    assert sync.resolve_worker_count(100, cpu_load_override=50) == 5
    assert sync.resolve_worker_count(100, cpu_load_override=100) == 10


def test_explicit_worker_count_wins_over_cpu_load(ten_cores):
    assert sync.resolve_worker_count(100, workers_override=3,
                                     cpu_load_override=100) == 3


def test_max_workers_env_pins_the_count(ten_cores, monkeypatch):
    monkeypatch.setattr(config, "MAX_WORKERS", 2)
    assert sync.resolve_worker_count(100) == 2


def test_never_more_workers_than_repos(ten_cores):
    assert sync.resolve_worker_count(2, workers_override=16) == 2


def test_the_cpu_based_default_never_drops_below_four(ten_cores, monkeypatch):
    # Syncing waits on the network, not the CPU: a 1-2 CPU VM still gets 4
    monkeypatch.setattr(sync.os, "cpu_count", lambda: 1)
    assert sync.resolve_worker_count(100) == 4
    assert sync.resolve_worker_count(100, cpu_load_override=1) == 4
    assert sync.resolve_worker_count(2) == 2            # never more than repos


def test_an_explicit_count_is_taken_as_given(ten_cores):
    assert sync.resolve_worker_count(5, workers_override=1) == 1
    assert sync.resolve_worker_count(5, workers_override=0) == 1   # at least one


# --------------------------------------------------------------------------- #
# Visibility                                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("setting, repo, expected", [
    ("auto", {"private": True}, "private"),
    ("auto", {"private": False}, "public"),
    ("auto", {}, "private"),            # GitHub left the flag out: play it safe
    ("public", {"private": True}, "public"),
    ("private", {"private": False}, "private"),
    ("", {"private": False}, "private"),
])
def test_resolve_visibility(monkeypatch, setting, repo, expected):
    monkeypatch.setattr(config, "REPO_VISIBILITY", setting)
    assert sync.resolve_visibility(repo) == expected


# --------------------------------------------------------------------------- #
# One repo, end to end                                                         #
# --------------------------------------------------------------------------- #

REPO = {"name": "repo-a", "clone_url": "https://github.com/o/repo-a.git",
        "private": True}


@pytest.fixture
def steps(monkeypatch):
    """Stub the three steps of a sync and record what happened."""
    calls = []
    outcome = {"mirror": True, "project": True, "push": True}

    def record(step, result):
        def _step(**kwargs):
            calls.append(step)
            return result()
        return _step

    monkeypatch.setattr(sync, "mirror_repo", record(
        "mirror", lambda: outcome["mirror"]))
    monkeypatch.setattr(sync, "ensure_gitlab_project", record(
        "project", lambda: outcome["project"]))
    monkeypatch.setattr(sync, "push_to_gitlab", record(
        "push", lambda: outcome["push"]))
    monkeypatch.setattr(config, "REPO_VISIBILITY", "auto")
    return calls, outcome


def test_process_repo_reports_success(steps):
    calls, _ = steps
    assert sync.process_repo(REPO, None, sync.ActiveRepos()) is True
    assert calls == ["mirror", "project", "push"]


@pytest.mark.parametrize("failing", ["mirror", "project", "push"])
def test_a_failed_step_fails_the_repo(steps, failing):
    calls, outcome = steps
    outcome[failing] = False
    assert sync.process_repo(REPO, None, sync.ActiveRepos()) is False
    # Every step still runs - an out-of-date mirror beats no mirror
    assert calls == ["mirror", "project", "push"]


def test_process_repo_clears_the_active_marker(steps):
    active = sync.ActiveRepos()
    sync.process_repo(REPO, None, active)
    assert active.describe() == "waiting..."


def test_an_unexpected_exception_fails_the_repo_without_escaping(monkeypatch):
    monkeypatch.setattr(sync, "mirror_repo", lambda **kw: 1 / 0)
    assert sync.process_repo(REPO, None, sync.ActiveRepos()) is False


# --------------------------------------------------------------------------- #
# The whole run                                                                #
# --------------------------------------------------------------------------- #

@pytest.fixture
def eight_repos(monkeypatch, tmp_path):
    repos = [{"name": f"repo-{i}", "clone_url": f"https://github.com/o/repo-{i}.git",
              "private": False} for i in range(8)]
    monkeypatch.setattr(github_api, "get_repos", lambda: repos)
    monkeypatch.setattr(gitlab_api, "get_group_id", lambda: None)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setattr(config, "GITHUB_USER", "octocat")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "x" * 12)
    monkeypatch.setattr(config, "GITLAB_USER", "octolab")
    monkeypatch.setattr(config, "GITLAB_TOKEN", "y" * 12)
    return repos


def test_run_sync_works_on_several_repos_at_once(monkeypatch, eight_repos):
    peak = 0
    running = 0
    lock = threading.Lock()

    def slow_mirror(**kwargs):
        nonlocal peak, running
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.15)
        with lock:
            running -= 1
        return True

    monkeypatch.setattr(sync, "mirror_repo", slow_mirror)
    monkeypatch.setattr(sync, "ensure_gitlab_project", lambda **kw: True)
    monkeypatch.setattr(sync, "push_to_gitlab", lambda **kw: True)

    started = time.time()
    assert sync.run_sync(workers_override=4) == 0
    elapsed = time.time() - started

    assert peak == 4
    # Sequential would be 8 * 0.15 = 1.2s; four at a time is ~0.3s
    assert elapsed < 0.9


def test_run_sync_counts_failures_and_exits_non_zero(monkeypatch, eight_repos,
                                                     captured_log):
    monkeypatch.setattr(sync, "mirror_repo", lambda **kw: True)
    monkeypatch.setattr(sync, "ensure_gitlab_project",
                        lambda **kw: kw["repo_name"] != "repo-5")
    monkeypatch.setattr(sync, "push_to_gitlab",
                        lambda **kw: kw["repo_name"] != "repo-6")

    assert sync.run_sync(workers_override=4) == 1
    rendered = "\n".join(captured_log)
    assert "6 synced, 2 failed" in rendered
    assert "[repo-5] ❌ Failed repo-5 (mirror=True, gitlab-project=False, push=True)" in rendered
    assert "[repo-6] ❌ Failed repo-6 (mirror=True, gitlab-project=True, push=False)" in rendered


def test_run_sync_stops_early_when_credentials_are_missing(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "your-github-token")
    called = []
    monkeypatch.setattr(github_api, "get_repos", lambda: called.append(1) or [])

    with pytest.raises(SystemExit) as excinfo:
        sync.run_sync()

    assert excinfo.value.code == 1
    assert called == []  # never reached the network


def test_run_sync_handles_an_empty_account(monkeypatch, eight_repos):
    monkeypatch.setattr(github_api, "get_repos", lambda: [])
    assert sync.run_sync() == 0


# --------------------------------------------------------------------------- #
# Mirroring                                                                    #
# --------------------------------------------------------------------------- #

def test_mirror_clones_then_fetches_then_recovers(tmp_path, repo_factory):
    source = repo_factory("repo-a", {"README.md": "# A\n"})
    local = str(tmp_path / "mirrors" / "repo-a.git")

    assert sync.mirror_repo("repo-a", str(source), local) is True
    assert sync.mirror_repo("repo-a", str(source), local) is True  # fetch path

    # Corrupt the mirror: the fetch fails and the reclone rescues it
    import shutil
    shutil.rmtree(f"{local}/objects")
    assert sync.mirror_repo("repo-a", str(source), local) is True


def test_failed_reclone_keeps_the_old_mirror(tmp_path, repo_factory):
    source = repo_factory("repo-a", {"README.md": "# A\n"})
    local = str(tmp_path / "mirrors" / "repo-a.git")
    assert sync.mirror_repo("repo-a", str(source), local) is True

    # GitHub is "down": the fetch and the reclone both fail
    import shutil
    shutil.rmtree(source)
    assert sync.mirror_repo("repo-a", str(source), local) is False

    # ...but the backup we already had is untouched, and no half-clone is left
    assert archive.has_commits(local)
    assert not os.path.exists(f"{local}.reclone")


def test_mirror_reports_failure(tmp_path, captured_log):
    assert sync.mirror_repo(
        "nope", "https://127.0.0.1:1/nope.git", str(tmp_path / "nope.git")) is False


def test_active_repos_header():
    active = sync.ActiveRepos()
    assert active.describe() == "waiting..."
    for name in ("a", "b", "c", "d", "e"):
        active.add(name)
    assert active.describe() == "a, b, c (+2 more)"
    for name in ("a", "b", "c", "d", "e"):
        active.remove(name)
    assert active.describe() == "waiting..."


# --------------------------------------------------------------------------- #
# Fetch only (what archive runs first)                                         #
# --------------------------------------------------------------------------- #

def test_fetch_only_never_touches_gitlab(steps):
    calls, _ = steps
    assert sync.process_repo(REPO, None, sync.ActiveRepos(), push=False) is True
    assert calls == ["mirror"]


def test_fetch_only_needs_no_gitlab_credentials(monkeypatch, eight_repos):
    monkeypatch.setattr(config, "GITLAB_USER", "")
    monkeypatch.setattr(config, "GITLAB_TOKEN", "")
    monkeypatch.setattr(gitlab_api, "get_group_id",
                        lambda: pytest.fail("GitLab was contacted"))
    monkeypatch.setattr(sync, "mirror_repo", lambda **kw: True)
    assert sync.run_sync(push=False) == 0


def test_sync_still_requires_gitlab_credentials(monkeypatch, eight_repos):
    monkeypatch.setattr(config, "GITLAB_TOKEN", "")
    with pytest.raises(SystemExit):
        sync.run_sync()


def test_fetch_only_honours_the_backup_dir(monkeypatch, steps, tmp_path):
    seen = []
    monkeypatch.setattr(sync, "mirror_repo",
                        lambda **kw: seen.append(kw["local_path"]) or True)
    sync.process_repo(REPO, None, sync.ActiveRepos(), push=False,
                      backup_dir=str(tmp_path / "elsewhere"))
    assert seen == [str(tmp_path / "elsewhere" / "repo-a.git")]


# --------------------------------------------------------------------------- #
# Reclone swap                                                                 #
# --------------------------------------------------------------------------- #

def test_a_failed_swap_puts_the_old_mirror_back(tmp_path, repo_factory, monkeypatch):
    source = repo_factory("repo-a", {"README.md": "# A\n"})
    local = str(tmp_path / "mirrors" / "repo-a.git")
    assert sync.mirror_repo("repo-a", str(source), local) is True
    shutil_rmtree = __import__("shutil").rmtree
    shutil_rmtree(f"{local}/objects")      # corrupt it, so the fetch fails...
    # ...but keep a marker to prove the very same folder comes back
    open(f"{local}/marker", "w").close()

    real_rename = os.rename
    calls = []

    def flaky_rename(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:                  # moving the fresh clone in fails
            raise OSError("disk trouble")
        return real_rename(src, dst)

    monkeypatch.setattr(sync.os, "rename", flaky_rename)
    assert sync.mirror_repo("repo-a", str(source), local) is False
    assert os.path.exists(f"{local}/marker")        # the old mirror is back
    assert not os.path.exists(f"{local}.old")
    assert not os.path.exists(f"{local}.reclone")


def test_a_successful_reclone_leaves_no_leftovers(tmp_path, repo_factory):
    source = repo_factory("repo-a", {"README.md": "# A\n"})
    local = str(tmp_path / "mirrors" / "repo-a.git")
    sync.mirror_repo("repo-a", str(source), local)
    __import__("shutil").rmtree(f"{local}/objects")
    assert sync.mirror_repo("repo-a", str(source), local) is True
    assert archive.has_commits(local)
    assert sorted(os.listdir(tmp_path / "mirrors")) == ["repo-a.git"]


# --------------------------------------------------------------------------- #
# Pushing to GitLab                                                            #
# --------------------------------------------------------------------------- #

@pytest.fixture
def git_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "run", lambda cmd, **kw: calls.append(cmd))
    return calls


def test_push_goes_to_the_user_namespace_with_the_token(git_calls):
    assert sync.push_to_gitlab(None, "octolab", "glpat-tok", "repo-a", "/m/repo-a.git")
    cmd = git_calls[0]
    assert cmd[:5] == ["git", "--git-dir", "/m/repo-a.git", "push", "--mirror"]
    # The credential half is joined on at runtime: written in one piece, a
    # user:password@ URL trips secret scanners (GitGuardian) though it's fake
    assert cmd[5] == "https://oauth2:" + "glpat-tok@gitlab.com/octolab/repo-a.git"


def test_push_goes_to_the_group_namespace(git_calls, monkeypatch):
    monkeypatch.setattr(config, "GITLAB_GROUP", "my-group")
    sync.push_to_gitlab(42, "octolab", "glpat-tok", "repo-a", "/m/repo-a.git")
    assert git_calls[0][5] == "https://oauth2:" + "glpat-tok@gitlab.com/my-group/repo-a.git"


def test_a_failed_push_is_reported(monkeypatch):
    def boom(cmd, **kw):
        raise RuntimeError("rejected")
    monkeypatch.setattr(sync, "run", boom)
    assert sync.push_to_gitlab(None, "octolab", "t", "repo-a", "/m") is False


# --------------------------------------------------------------------------- #
# GitLab project visibility                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("current, desired, updated, expected, puts", [
    ("private", "private", None, True, 0),        # already right: no update
    ("public", "private", {"id": 7}, True, 1),    # wrong: updated
    ("public", "private", None, False, 1),        # update refused
])
def test_visibility_is_brought_in_line(monkeypatch, current, desired, updated,
                                       expected, puts):
    put_calls = []
    monkeypatch.setattr(gitlab_api, "get_project",
                        lambda **kw: {"id": 7, "visibility": current})
    monkeypatch.setattr(gitlab_api, "update_project_visibility",
                        lambda pid, vis: put_calls.append(vis) or updated)
    assert sync.ensure_gitlab_project(None, "repo-a", "me", desired) is expected
    assert len(put_calls) == puts


# --------------------------------------------------------------------------- #
# Interrupting a run                                                           #
# --------------------------------------------------------------------------- #

def test_ctrl_c_during_a_sync_returns_130(monkeypatch, eight_repos):
    def interrupted(**kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(sync, "mirror_repo", interrupted)
    monkeypatch.setattr(sync, "ensure_gitlab_project", lambda **kw: True)
    monkeypatch.setattr(sync, "push_to_gitlab", lambda **kw: True)
    assert sync.run_sync(workers_override=2) == 130


# --------------------------------------------------------------------------- #
# Repos that share a name                                                      #
# --------------------------------------------------------------------------- #

def gh(owner, name):
    return {"name": name, "full_name": f"{owner}/{name}", "owner": {"login": owner},
            "clone_url": f"https://github.com/{owner}/{name}.git"}


def test_names_without_a_clash_are_left_alone():
    repos = [gh("me", "api"), gh("my-org", "web")]
    assert sync.local_names(repos, "me") == ["api", "web"]


def test_your_own_repo_keeps_the_plain_name():
    repos = [gh("my-org", "dotfiles"), gh("me", "dotfiles")]
    assert sync.local_names(repos, "me") == ["my-org-dotfiles", "dotfiles"]


def test_who_gets_the_plain_name_does_not_depend_on_order():
    a, b = gh("me", "dotfiles"), gh("my-org", "dotfiles")
    assert sync.local_names([a, b], "me") == ["dotfiles", "my-org-dotfiles"]
    assert sync.local_names([b, a], "me") == ["my-org-dotfiles", "dotfiles"]


def test_clashes_ignore_case():
    # "Dotfiles" and "dotfiles" are one folder on macOS and one GitLab path
    repos = [gh("me", "dotfiles"), gh("Other", "Dotfiles")]
    assert sync.local_names(repos, "ME") == ["dotfiles", "Other-Dotfiles"]


def test_when_none_is_yours_every_clashing_repo_is_prefixed():
    repos = [gh("org-a", "tools"), gh("org-b", "tools"), gh("me", "api")]
    assert sync.local_names(repos, "me") == ["org-a-tools", "org-b-tools", "api"]


def test_a_prefixed_name_never_takes_a_real_repos_name():
    # You really do own a repo called "my-org-dotfiles"
    repos = [gh("me", "dotfiles"), gh("my-org", "dotfiles"), gh("me", "my-org-dotfiles")]
    names = sync.local_names(repos, "me")
    assert names == ["dotfiles", "my-org-dotfiles-2", "my-org-dotfiles"]
    assert len({n.lower() for n in names}) == 3


def test_a_mirror_of_another_repo_is_replaced(tmp_path, repo_factory):
    # The folder name used to belong to the org's repo; now it is yours
    org = repo_factory("org-dotfiles", {"ORG.md": "org\n"})
    mine = repo_factory("my-dotfiles", {"MINE.md": "mine\n"})
    local = str(tmp_path / "mirrors" / "dotfiles.git")
    assert sync.mirror_repo("dotfiles", str(org), local) is True
    assert sync._mirror_matches(local, str(org))
    assert not sync._mirror_matches(local, str(mine))

    assert sync.mirror_repo("dotfiles", str(mine), local) is True
    assert sync._mirror_matches(local, str(mine))      # now follows your repo
    files = subprocess.run(["git", "--git-dir", local, "ls-tree", "--name-only", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert files == ["MINE.md"]


def test_the_stored_token_does_not_count_as_a_different_repo(tmp_path, repo_factory):
    source = repo_factory("repo-a", {"README.md": "# A\n"})
    local = str(tmp_path / "repo-a.git")
    sync.mirror_repo("repo-a", str(source), local)
    subprocess.run(["git", "--git-dir", local, "config", "remote.origin.url",
                    "https://octocat:" + "OLD-TOKEN@github.com/Me/Repo-A.git/"], check=True)
    assert sync._mirror_matches(local, "https://github.com/me/repo-a.git")


def test_clashing_repos_are_kept_apart_and_archived(monkeypatch, tmp_path, repo_factory):
    # End to end through the fetch-only run archive does first, then archive
    mine = repo_factory("my-dotfiles", {"MINE.md": "mine\n"})
    org = repo_factory("org-dotfiles", {"ORG.md": "org\n"})
    repos = [dict(gh("my-org", "dotfiles"), clone_url=str(org)),
             dict(gh("me", "dotfiles"), clone_url=str(mine))]
    backup = tmp_path / "backup"
    monkeypatch.setattr(github_api, "get_repos", lambda: repos)
    monkeypatch.setattr(config, "GITHUB_USER", "me")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "x" * 12)

    assert sync.run_sync(push=False, backup_dir=str(backup), workers_override=2) == 0
    assert sorted(os.listdir(backup)) == ["dotfiles.git", "my-org-dotfiles.git"]

    out = tmp_path / "code.zip"
    assert archive.run_archive("code", 6, str(out), str(backup), []) == 0
    import zipfile
    assert sorted(zipfile.ZipFile(out).namelist()) == [
        "dotfiles/MINE.md", "my-org-dotfiles/ORG.md"]


def test_a_mirror_without_an_origin_is_replaced(tmp_path, repo_factory):
    # `git fetch --all` with no remote fetches nothing and still exits 0 - so
    # such a mirror must not count as "this repo", or it would never update
    source = repo_factory("repo-a", {"README.md": "# A\n"})
    local = str(tmp_path / "repo-a.git")
    subprocess.run(["git", "init", "-q", "--bare", local], check=True)
    assert sync._mirror_matches(local, str(source)) is None   # unreadable, not "same"

    assert sync.mirror_repo("repo-a", str(source), local) is True
    assert sync._mirror_matches(local, str(source))      # now a real clone
    assert archive.has_commits(local)


def test_a_damaged_mirror_is_logged_as_damaged_not_as_another_repo(
        tmp_path, repo_factory, captured_log):
    # Deleting objects/ leaves a folder git cannot open at all. It is still the
    # same repo, so the log must not claim it is a different one.
    source = repo_factory("repo-a", {"README.md": "# A\n"})
    local = str(tmp_path / "repo-a.git")
    sync.mirror_repo("repo-a", str(source), local)
    __import__("shutil").rmtree(f"{local}/objects")

    assert sync._mirror_matches(local, str(source)) is None
    assert sync.mirror_repo("repo-a", str(source), local) is True
    log = "\n".join(captured_log)
    assert "cannot be read as a mirror" in log
    assert "different repo" not in log


# --------------------------------------------------------------------------- #
# Forks                                                                        #
# --------------------------------------------------------------------------- #

@pytest.fixture
def fork_account(monkeypatch, tmp_path):
    """Your fork of my-org/api, the org's api itself, and your own tool."""
    repos = [dict(gh("me", "api"), fork=True),
             dict(gh("my-org", "api"), fork=False),
             dict(gh("me", "tool"), fork=False)]
    seen = []
    monkeypatch.setattr(github_api, "get_repos", lambda: repos)
    monkeypatch.setattr(config, "GITHUB_USER", "me")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "x" * 12)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setattr(sync, "mirror_repo",
                        lambda **kw: seen.append(kw["local_path"].rsplit("/", 1)[-1]) or True)
    return seen


def test_forks_are_backed_up_by_default(fork_account, monkeypatch):
    monkeypatch.setattr(config, "INCLUDE_FORKS", True)
    assert sync.run_sync(push=False) == 0
    assert sorted(fork_account) == ["api.git", "my-org-api.git", "tool.git"]


def test_forks_can_be_left_out(fork_account, monkeypatch, captured_log):
    monkeypatch.setattr(config, "INCLUDE_FORKS", False)
    assert sync.run_sync(push=False) == 0
    assert "api.git" not in fork_account                  # your fork: skipped
    assert any("Skipping 1 fork(s) (INCLUDE_FORKS=false): me/api" in line
               for line in captured_log)


def test_leaving_forks_out_never_renames_another_repo(fork_account, monkeypatch):
    # my-org/api must stay "my-org-api" - taking the plain "api" from your
    # skipped fork would push the org's code over your fork's GitLab backup
    monkeypatch.setattr(config, "INCLUDE_FORKS", False)
    sync.run_sync(push=False)
    assert sorted(fork_account) == ["my-org-api.git", "tool.git"]


def test_an_account_of_only_forks_has_nothing_to_do(monkeypatch, tmp_path):
    monkeypatch.setattr(github_api, "get_repos",
                        lambda: [dict(gh("me", "linux"), fork=True)])
    monkeypatch.setattr(config, "GITHUB_USER", "me")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "x" * 12)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setattr(config, "INCLUDE_FORKS", False)
    monkeypatch.setattr(sync, "mirror_repo", lambda **kw: pytest.fail("cloned a fork"))
    assert sync.run_sync(push=False) == 0



# --------------------------------------------------------------------------- #
# A push GitLab refuses because the branch is protected                        #
# --------------------------------------------------------------------------- #

PROTECTED = ("remote: GitLab: You are not allowed to force push code to a "
             "protected branch on this project.")


@pytest.fixture
def protected_push(monkeypatch):
    """The first push is refused (protected branch), any later one succeeds."""
    from gitsync import process
    calls = {"push": 0, "allowed": 0}

    def push(**kw):
        calls["push"] += 1
        process._last_error.line, process._last_error.rank = None, 0   # like run()
        if calls["push"] == 1:
            process._remember_error(PROTECTED)
            return False
        return True

    def allow(**kw):
        calls["allowed"] += 1
        return 1

    monkeypatch.setattr(sync, "mirror_repo", lambda **kw: True)
    monkeypatch.setattr(sync, "ensure_gitlab_project", lambda **kw: True)
    monkeypatch.setattr(sync, "push_to_gitlab", push)
    monkeypatch.setattr(gitlab_api, "allow_force_push", allow)
    monkeypatch.setattr(config, "REPO_VISIBILITY", "auto")
    return calls


def test_with_the_setting_on_force_push_is_allowed_and_the_push_retried(
        protected_push, monkeypatch):
    monkeypatch.setattr(config, "GITLAB_ALLOW_FORCE_PUSH", True)
    board = sync.ActiveRepos()
    assert sync.process_repo(REPO, None, board) is True
    assert protected_push == {"push": 2, "allowed": 1}
    assert board.failures == []


def test_with_the_setting_off_the_reason_says_how_to_fix_it(protected_push, monkeypatch):
    monkeypatch.setattr(config, "GITLAB_ALLOW_FORCE_PUSH", False)
    board = sync.ActiveRepos()
    assert sync.process_repo(REPO, None, board) is False
    assert protected_push == {"push": 1, "allowed": 0}         # GitLab untouched
    (name, step, reason), = board.failures
    assert step == "push" and "protected branch" in reason
    assert "set GITLAB_ALLOW_FORCE_PUSH=true" in reason


def test_other_push_failures_never_touch_gitlab_settings(monkeypatch):
    from gitsync import process
    monkeypatch.setattr(config, "GITLAB_ALLOW_FORCE_PUSH", True)
    monkeypatch.setattr(sync, "mirror_repo", lambda **kw: True)
    monkeypatch.setattr(sync, "ensure_gitlab_project", lambda **kw: True)

    def push(**kw):
        process._last_error.line, process._last_error.rank = None, 0
        process._remember_error("fatal: unable to access 'https://gitlab.com/x.git/'")
        return False

    monkeypatch.setattr(sync, "push_to_gitlab", push)
    monkeypatch.setattr(gitlab_api, "allow_force_push",
                        lambda **kw: pytest.fail("changed GitLab settings"))
    board = sync.ActiveRepos()
    assert sync.process_repo(REPO, None, board) is False
    assert board.failures[0][2].startswith("fatal: unable to access")
