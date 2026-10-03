"""run() is the one place a live token reaches a command line."""

import subprocess

import pytest

from gitsync.process import run

from .conftest import FAKE_GITHUB_TOKEN

pytestmark = pytest.mark.usefixtures("known_secrets")

AUTH_URL = f"https://octocat:{FAKE_GITHUB_TOKEN}@127.0.0.1:1/nope.git"


def test_streams_output_into_the_log(captured_log):
    run(["echo", "hello from git"])
    assert "INFO RUN: echo hello from git (cwd=.)" in captured_log
    assert "INFO hello from git" in captured_log


def test_logs_the_masked_command(captured_log):
    run(["echo", AUTH_URL])
    rendered = "\n".join(captured_log)
    assert FAKE_GITHUB_TOKEN not in rendered
    assert "https://octocat:***@127.0.0.1:1/nope.git" in rendered


def test_raises_with_a_masked_command(captured_log, tmp_path):
    # A real clone that cannot connect: the failure carries the auth URL
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        run(["git", "clone", "--mirror", AUTH_URL, str(tmp_path / "clone")])

    assert FAKE_GITHUB_TOKEN not in str(excinfo.value)
    assert "https://octocat:***@127.0.0.1:1/nope.git" in str(excinfo.value)
    assert FAKE_GITHUB_TOKEN not in "\n".join(captured_log)


def test_check_false_swallows_the_exit_code():
    run(["sh", "-c", "exit 3"], check=False)


def test_terminal_prompt_is_disabled(monkeypatch):
    # An unattended/parallel run must never block on a credential prompt
    seen = {}

    class _Proc:
        stdout = iter(())
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs["env"])
        return _Proc()

    monkeypatch.setattr("gitsync.process.subprocess.Popen", fake_popen)
    run(["git", "status"])
    assert seen["GIT_TERMINAL_PROMPT"] == "0"


def test_a_failure_while_reading_kills_the_child(monkeypatch):
    # If logging a line blows up, the git process must not be left running
    import time
    from gitsync import process

    def broken_info(msg, *args):
        if not msg.startswith("RUN:"):
            raise RuntimeError("log handler broke")

    monkeypatch.setattr(process.logger, "info", broken_info)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="log handler broke"):
        run(["sh", "-c", "echo first line; sleep 30"])
    # Had the child been left alone, wait() would have sat out the 30 s
    assert time.monotonic() - started < 10


def test_git_gives_up_on_a_stalled_transfer(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen.update(kwargs["env"])
            self.stdout = iter(())
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    run(["git", "fetch"])
    assert seen["GIT_HTTP_LOW_SPEED_LIMIT"] == "1000"
    assert seen["GIT_HTTP_LOW_SPEED_TIME"] == "120"
