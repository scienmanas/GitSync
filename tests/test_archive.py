"""Issue #19: bundle the local mirrors into one zip, with or without the
vendored dependencies.
"""

import os
import subprocess
import zipfile

import pytest

from gitsync import archive


# --------------------------------------------------------------------------- #
# What counts as a dependency                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "node_modules/left-pad/index.js",
    "frontend/node_modules/react/index.js",
    ".venv/lib/python3.12/site-packages/requests/__init__.py",
    "app/__pycache__/main.cpython-312.pyc",
    "src/cached.pyc",
    "dist/bundle.js",
    "target/debug/app",
    "vendor/autoload.php",
])
def test_dependency_paths_are_recognised(path):
    assert archive.is_dependency_path(path) is True


@pytest.mark.parametrize("path", [
    "README.md",
    "src/app.py",
    "src/distributed/queue.py",     # "dist" only matches a whole folder
    "docs/building.md",
    "outbox/handler.go",
])
def test_real_code_is_kept(path):
    assert archive.is_dependency_path(path) is False


def test_extra_excludes_are_honoured():
    assert archive.is_dependency_path("docs/big.pdf", ("*.pdf",)) is True
    assert archive.is_dependency_path("fixtures/a.json", ("fixtures",)) is True
    assert archive.is_dependency_path("src/app.py", ("*.pdf",)) is False


def test_dependency_dirs_falls_back_to_the_default_list(monkeypatch):
    monkeypatch.setattr(archive.config, "CODE_ARCHIVE_EXCLUDE_DIRS", None)
    assert archive.dependency_dirs() == archive.DEFAULT_DEPENDENCY_DIRS


def test_code_archive_exclude_dirs_replaces_the_default_list(monkeypatch):
    # CODE_ARCHIVE_EXCLUDE_DIRS in .env replaces the built-in list rather than
    # adding to it, so leaving a folder out of it (e.g. "dist") keeps it.
    monkeypatch.setattr(archive.config, "CODE_ARCHIVE_EXCLUDE_DIRS", ("vendor",))
    assert archive.dependency_dirs() == frozenset({"vendor"})
    assert archive.is_dependency_path("vendor/autoload.php") is True
    assert archive.is_dependency_path("dist/bundle.js") is False


# --------------------------------------------------------------------------- #
# Finding the mirrors                                                          #
# --------------------------------------------------------------------------- #

def test_find_mirrors_lists_repos_without_the_git_suffix(mirrors_dir):
    assert [name for name, _ in archive.find_mirrors(str(mirrors_dir))] == [
        "empty-repo", "repo-a", "repo-b"]


def test_find_mirrors_tolerates_a_missing_folder(tmp_path):
    assert archive.find_mirrors(str(tmp_path / "nope")) == []


def test_archive_without_mirrors_exits_non_zero(tmp_path):
    assert archive.run_archive("full", 6, str(tmp_path / "out.zip"),
                               str(tmp_path / "nope"), []) == 1


# --------------------------------------------------------------------------- #
# code mode                                                                    #
# --------------------------------------------------------------------------- #

def test_code_mode_keeps_only_the_source(mirrors_dir, tmp_path):
    out = tmp_path / "code.zip"
    assert archive.run_archive("code", 6, str(out), str(mirrors_dir), []) == 0

    assert sorted(zipfile.ZipFile(out).namelist()) == [
        "repo-a/README.md", "repo-a/src/app.py", "repo-b/README.md"]


def test_code_mode_skips_a_repo_with_no_commits(mirrors_dir, tmp_path,
                                                captured_log):
    out = tmp_path / "code.zip"
    archive.run_archive("code", 6, str(out), str(mirrors_dir), [])

    assert not any(n.startswith("empty-repo/")
                   for n in zipfile.ZipFile(out).namelist())
    assert any("empty-repo - the mirror has no commits yet" in line
               for line in captured_log)


def test_code_mode_applies_extra_excludes(mirrors_dir, tmp_path):
    out = tmp_path / "code.zip"
    archive.run_archive("code", 6, str(out), str(mirrors_dir), ["README.md"])
    assert zipfile.ZipFile(out).namelist() == ["repo-a/src/app.py"]


def test_code_mode_preserves_permissions_and_dates(mirrors_dir, tmp_path):
    out = tmp_path / "code.zip"
    archive.run_archive("code", 6, str(out), str(mirrors_dir), [])

    info = zipfile.ZipFile(out).getinfo("repo-a/src/app.py")
    assert info.external_attr >> 16 & 0o777 in (0o644, 0o664, 0o755)
    assert info.date_time[0] >= 1980


# --------------------------------------------------------------------------- #
# full mode                                                                    #
# --------------------------------------------------------------------------- #

def test_full_mode_round_trips_through_git_clone(mirrors_dir, tmp_path):
    """The point of a backup: you can get your repo back out of it."""
    out = tmp_path / "full.zip"
    assert archive.run_archive("full", 6, str(out), str(mirrors_dir), []) == 0

    extracted = tmp_path / "extracted"
    zipfile.ZipFile(out).extractall(extracted)

    work = tmp_path / "restored"
    subprocess.run(["git", "clone", "-q", str(extracted / "repo-a.git"),
                    str(work)], check=True, capture_output=True)

    assert (work / "README.md").exists()
    # Whatever was committed comes back, vendored dependencies included
    assert (work / "node_modules/left-pad/index.js").exists()
    log = subprocess.run(["git", "-C", str(work), "log", "--oneline"],
                         check=True, capture_output=True, text=True)
    assert "init" in log.stdout


def test_full_mode_stores_empty_git_folders(mirrors_dir, tmp_path):
    # git refuses to open a repo whose empty refs/ did not survive the zip
    out = tmp_path / "full.zip"
    archive.run_archive("full", 6, str(out), str(mirrors_dir), [])
    assert "repo-a.git/refs/" in zipfile.ZipFile(out).namelist()


# --------------------------------------------------------------------------- #
# Compression                                                                  #
# --------------------------------------------------------------------------- #

def test_compression_zero_stores_without_deflating(mirrors_dir, tmp_path):
    out = tmp_path / "stored.zip"
    archive.run_archive("code", 0, str(out), str(mirrors_dir), [])
    assert all(info.compress_type == zipfile.ZIP_STORED
               for info in zipfile.ZipFile(out).infolist())


def test_higher_levels_deflate(mirrors_dir, tmp_path):
    out = tmp_path / "deflated.zip"
    archive.run_archive("code", 9, str(out), str(mirrors_dir), [])
    assert all(info.compress_type == zipfile.ZIP_DEFLATED
               for info in zipfile.ZipFile(out).infolist())


def test_default_output_lands_in_the_archive_dir(mirrors_dir, tmp_path,
                                                 monkeypatch):
    monkeypatch.setattr(archive.config, "ARCHIVE_DIR", str(tmp_path / "arch"))
    assert archive.run_archive("code", 6, None, str(mirrors_dir), []) == 0

    written = list((tmp_path / "arch").iterdir())
    assert len(written) == 1
    assert written[0].name.startswith("gitsync-code-")
    assert written[0].suffix == ".zip"


def test_human_size():
    assert archive.human_size(0) == "0.0 B"
    assert archive.human_size(2048) == "2.0 KB"
    assert archive.human_size(5 * 1024 ** 3) == "5.0 GB"


# --------------------------------------------------------------------------- #
# Failures are reported, not hidden                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["build", "scripts/env", "bin/out"])
def test_a_file_named_like_a_dependency_folder_is_kept(path):
    # The built-in names are folders; a script called `build` is source
    assert archive.is_dependency_path(path) is False


def test_a_repo_that_fails_to_archive_fails_the_run(mirrors_dir, tmp_path,
                                                     monkeypatch):
    real = archive.add_code_snapshot

    def flaky(zf, git_dir, repo_name, *args):
        if repo_name == "repo-b":
            raise RuntimeError("git archive failed: boom")
        return real(zf, git_dir, repo_name, *args)

    monkeypatch.setattr(archive, "add_code_snapshot", flaky)
    out = tmp_path / "code.zip"
    assert archive.run_archive("code", 6, str(out), str(mirrors_dir), []) == 1
    # ...but what did work is still written - most of a backup beats none
    assert "repo-a/README.md" in zipfile.ZipFile(out).namelist()


def test_a_broken_git_archive_raises_instead_of_passing(tmp_path):
    broken = tmp_path / "broken.git"
    subprocess.run(["git", "init", "-q", "--bare", str(broken)], check=True)
    # HEAD points at a commit, but the object it names is not there
    (broken / "refs" / "heads" / "main").write_text("0" * 39 + "1\n")
    (broken / "HEAD").write_text("ref: refs/heads/main\n")
    with zipfile.ZipFile(tmp_path / "x.zip", "w") as zf, pytest.raises(Exception):
        archive.add_code_snapshot(zf, str(broken), "broken", [],
                                  zipfile.ZIP_DEFLATED, 6)


# --------------------------------------------------------------------------- #
# Interrupts, odd dates, odd files                                             #
# --------------------------------------------------------------------------- #

def test_an_interrupted_archive_removes_the_partial_zip(mirrors_dir, tmp_path,
                                                        monkeypatch):
    # What `docker stop` mid-archive relies on: no half-written zip left behind
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(archive, "add_full_mirror", interrupted)
    out = tmp_path / "full.zip"
    assert archive.run_archive("full", 6, str(out), str(mirrors_dir), []) == 130
    assert not out.exists()


def test_a_pre_1980_commit_date_is_clamped(tmp_path):
    # zip dates start in 1980; an older commit must not crash the archive
    work = tmp_path / "old"
    work.mkdir()
    (work / "a.txt").write_text("old\n")
    env = {**os.environ, "GIT_AUTHOR_DATE": "1975-06-01T12:00:00",
           "GIT_COMMITTER_DATE": "1975-06-01T12:00:00"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=work, env=env, check=True)
    mirrors = tmp_path / "mirrors"
    subprocess.run(["git", "clone", "-q", "--mirror", str(work),
                    str(mirrors / "old.git")], check=True)

    out = tmp_path / "code.zip"
    assert archive.run_archive("code", 6, str(out), str(mirrors), []) == 0
    assert zipfile.ZipFile(out).getinfo("old/a.txt").date_time[:3] == (1980, 1, 1)


def test_full_mode_never_follows_a_symlink(mirrors_dir, tmp_path):
    # A link in the mirror folder could point anywhere on the machine
    secret = tmp_path / "secret.txt"
    secret.write_text("not part of any repo\n")
    os.symlink(secret, mirrors_dir / "repo-b.git" / "sneaky")
    out = tmp_path / "full.zip"
    archive.run_archive("full", 6, str(out), str(mirrors_dir), [])
    assert "repo-b.git/sneaky" not in zipfile.ZipFile(out).namelist()


@pytest.mark.parametrize("num_bytes, expected", [
    (0, "0.0 B"), (1023, "1023.0 B"), (1024, "1.0 KB"),
    (5 * 1024 ** 3, "5.0 GB"), (3 * 1024 ** 5, "3072.0 TB"),
])
def test_human_size(num_bytes, expected):
    assert archive.human_size(num_bytes) == expected
