"""The formatter is where redaction actually bites: it sees the message, its
args and the traceback, so every handler is covered by construction.
"""

import logging
import subprocess
from collections import deque
from pathlib import Path

import pytest

from gitsync import logging_setup

from .conftest import FAKE_GITHUB_TOKEN, FAKE_GITLAB_TOKEN

pytestmark = pytest.mark.usefixtures("known_secrets")

logger = logging.getLogger("tests.logging")


def test_redacts_lazy_format_args(captured_log):
    logger.info("RUN: %s", f"git clone https://octocat:{FAKE_GITHUB_TOKEN}@github.com/o/r.git")
    assert captured_log == [
        "INFO RUN: git clone https://octocat:***@github.com/o/r.git"]


def test_redacts_tracebacks(captured_log):
    # CalledProcessError renders the whole command, auth URL included
    try:
        raise subprocess.CalledProcessError(
            128, ["git", "push", f"https://oauth2:{FAKE_GITLAB_TOKEN}@gitlab.com/o/r.git"])
    except subprocess.CalledProcessError as e:
        logger.exception("Push failed: %s", e)

    rendered = "\n".join(captured_log)
    assert FAKE_GITLAB_TOKEN not in rendered
    assert "Traceback (most recent call last)" in rendered
    assert "https://oauth2:***@gitlab.com/o/r.git" in rendered


def test_repo_prefix_follows_the_worker_thread(captured_log):
    logger.info("before")
    logging_setup.set_current_repo("my-repo")
    try:
        logger.info("during")
    finally:
        logging_setup.set_current_repo(None)
    logger.info("after")

    assert captured_log == ["INFO before", "INFO [my-repo] during", "INFO after"]


def test_rich_buffer_handler_keeps_the_last_lines():
    buffer = deque(maxlen=3)
    handler = logging_setup.RichBufferHandler(buffer)
    handler.setFormatter(logging_setup.SafeFormatter("%(message)s"))
    for i in range(5):
        handler.emit(logging.LogRecord(
            "t", logging.INFO, "f", 1, "line %d", (i,), None))
    assert list(buffer) == ["line 2", "line 3", "line 4"]


def test_rich_buffer_handler_splits_multiline_records():
    buffer = deque(maxlen=10)
    handler = logging_setup.RichBufferHandler(buffer)
    handler.setFormatter(logging_setup.SafeFormatter("%(message)s"))
    handler.emit(logging.LogRecord(
        "t", logging.INFO, "f", 1, "first\nsecond", None, None))
    assert list(buffer) == ["first", "second"]


def test_without_a_terminal_the_log_also_goes_to_stdout(tmp_path, monkeypatch,
                                                        capsys):
    # In a container there is no dashboard to read and `docker logs` only sees
    # stdout - and what lands there has to be redacted like everything else.
    monkeypatch.setattr(logging_setup.config, "LOGS_FOLDER", str(tmp_path))
    monkeypatch.setattr(logging_setup, "_log_file_path", None)
    root = logging.getLogger()
    existing = list(root.handlers)
    try:
        log_file = logging_setup.configure_logging()
        logger.info("cloning %s", f"https://o:{FAKE_GITHUB_TOKEN}@github.com/o/r.git")

        printed = capsys.readouterr().out
        assert "cloning https://o:***@github.com/o/r.git" in printed
        assert FAKE_GITHUB_TOKEN not in printed
        # and the file still gets it, redacted the same way
        assert FAKE_GITHUB_TOKEN not in Path(log_file).read_text()
    finally:
        for handler in list(root.handlers):
            if handler not in existing:
                root.removeHandler(handler)
                handler.close()


def test_excepthook_redacts_before_stderr(capsys):
    try:
        raise RuntimeError(f"crash with {FAKE_GITHUB_TOKEN}")
    except RuntimeError as e:
        logging_setup._redacting_excepthook(
            type(e), e, e.__traceback__)

    err = capsys.readouterr().err
    assert FAKE_GITHUB_TOKEN not in err
    assert "crash with ***" in err


def test_excepthook_redacts_a_ctrl_c_chained_onto_a_failure(capsys):
    # Ctrl-C - and SIGTERM, which cli.py turns into a KeyboardInterrupt - can
    # land inside an except block. Python then prints the error being handled
    # as well, so the interrupt path has to redact too.
    try:
        try:
            raise RuntimeError(f"push failed with {FAKE_GITLAB_TOKEN}")
        except RuntimeError:
            raise KeyboardInterrupt
    except KeyboardInterrupt as e:
        logging_setup._redacting_excepthook(type(e), e, e.__traceback__)

    err = capsys.readouterr().err
    assert FAKE_GITLAB_TOKEN not in err
    assert "push failed with ***" in err
    assert "KeyboardInterrupt" in err


# --------------------------------------------------------------------------- #
# One log file per scheduled run, and old ones aged out                        #
# --------------------------------------------------------------------------- #

@pytest.fixture
def fresh_logging(tmp_path, monkeypatch):
    """configure_logging into tmp_path, with every handler it adds removed after."""
    monkeypatch.setattr(logging_setup.config, "LOGS_FOLDER", str(tmp_path))
    monkeypatch.setattr(logging_setup, "_log_file_path", None)
    monkeypatch.setattr(logging_setup, "_file_handler", None)
    names = iter(f"logs_2026010{i}_000000.txt" for i in range(1, 10))
    monkeypatch.setattr(logging_setup, "_new_log_path",
                        lambda: str(tmp_path / next(names)))
    root = logging.getLogger()
    existing = list(root.handlers)
    yield tmp_path
    for handler in list(root.handlers):
        if handler not in existing:
            root.removeHandler(handler)
            handler.close()


def test_each_scheduled_run_gets_its_own_log_file(fresh_logging):
    first = logging_setup.configure_logging()
    logger.info("run one")
    second = logging_setup.start_new_log_file()
    logger.info("run two")

    assert first != second
    assert logging_setup.log_file_path() == second
    assert "run one" in Path(first).read_text()
    assert "run two" not in Path(first).read_text()     # the old file is closed off
    assert "run two" in Path(second).read_text()
    # exactly one of our file handlers left - the old one is not still
    # attached (pytest keeps a FileHandler of its own, outside our folder)
    ours = [h for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler)
            and h.baseFilename.startswith(str(fresh_logging))]
    assert [h.baseFilename for h in ours] == [second]


def test_a_new_log_file_before_logging_is_set_up_is_a_no_op(monkeypatch):
    monkeypatch.setattr(logging_setup, "_file_handler", None)
    monkeypatch.setattr(logging_setup, "_log_file_path", None)
    assert logging_setup.start_new_log_file() == "(logging not configured)"


def test_old_log_files_are_aged_out(fresh_logging, monkeypatch):
    import os
    import time
    folder = fresh_logging
    monkeypatch.setattr(logging_setup.config, "LOG_RETENTION_DAYS", 30)
    now = time.time()

    def make(name, days_old):
        path = folder / name
        path.write_text("x")
        os.utime(path, (now - days_old * 86400, now - days_old * 86400))
        return path

    old = make("logs_20250101_000000.txt", 40)
    recent = make("logs_20260901_000000.txt", 5)
    not_ours = make("notes.txt", 400)                    # never touched

    current = Path(logging_setup.configure_logging())    # prunes on start-up
    assert not old.exists()
    assert recent.exists() and not_ours.exists() and current.exists()


def test_retention_zero_keeps_every_log(fresh_logging, monkeypatch):
    import os
    monkeypatch.setattr(logging_setup.config, "LOG_RETENTION_DAYS", 0)
    ancient = fresh_logging / "logs_20200101_000000.txt"
    ancient.write_text("x")
    os.utime(ancient, (0, 0))
    logging_setup.configure_logging()
    assert ancient.exists()
