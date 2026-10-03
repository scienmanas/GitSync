"""Shared fixtures.

Nothing here talks to GitHub or GitLab - the tests that need a repo build a real
one with git in a tmp folder, and the tests that need the API stub it out.
"""

import logging
import subprocess

import pytest

from gitsync import redaction
from gitsync.logging_setup import SafeFormatter

# Tokens used throughout the tests. Fake, but shaped like the real thing - so
# each is built from two pieces: written as one string it looks like a real
# token to secret scanners (GitHub push protection, GitGuardian), which then
# block the push or flag the pull request.
FAKE_GITHUB_TOKEN = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
FAKE_GITLAB_TOKEN = "glpat-" + "SECRETSECRETSECRET1234"


@pytest.fixture
def known_secrets():
    """Register the fake tokens the way config does at import time."""
    added = []
    for token in (FAKE_GITHUB_TOKEN, FAKE_GITLAB_TOKEN):
        if token not in redaction._SECRET_VALUES:
            redaction.register_secret(token)
            added.append(token)
    yield
    for token in added:
        redaction._SECRET_VALUES.discard(token)


@pytest.fixture
def captured_log():
    """Capture log lines exactly as production renders them.

    pytest's own caplog formats records itself, which would bypass the
    redacting formatter - the very thing most of these tests are about.
    """
    lines = []

    class _Collector(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = _Collector()
    handler.setFormatter(SafeFormatter(
        "%(levelname)s %(repo_prefix)s%(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo_factory(tmp_path):
    """Build a real git repo, optionally carrying vendored dependencies."""

    def build(name, files, commit=True):
        work = tmp_path / "work" / name
        for relative, content in files.items():
            path = work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        work.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main", cwd=work)
        if commit:
            git("add", "-A", "-f", cwd=work)
            git("-c", "user.email=t@example.com", "-c", "user.name=Test",
                "commit", "-qm", "init", cwd=work)
        return work

    return build


@pytest.fixture
def mirrors_dir(tmp_path, repo_factory):
    """A backup folder holding mirrors, the way a sync would leave it.

    repo-a carries committed dependency/build junk, repo-b is plain, and
    empty-repo.git has no commits at all.
    """
    repo_a = repo_factory("repo-a", {
        "README.md": "# A\n",
        "src/app.py": "print('hi')\n",
        "src/cached.pyc": "compiled\n",
        "node_modules/left-pad/index.js": "module.exports = 1\n",
        "dist/bundle.js": "bundle\n",
    })
    repo_b = repo_factory("repo-b", {"README.md": "# B\n"})

    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    for repo in (repo_a, repo_b):
        git("clone", "-q", "--mirror", str(repo),
            str(mirrors / f"{repo.name}.git"))
    git("init", "-q", "--bare", str(mirrors / "empty-repo.git"))
    return mirrors
