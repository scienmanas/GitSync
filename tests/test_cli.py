"""Argument parsing and dispatch, including the bare `python main.py` that the
cron job in deploy.sh relies on.
"""

import contextlib
import os
import signal
import time

import pytest

from gitsync import cli


@pytest.fixture
def dispatch(monkeypatch):
    """Capture which command ran with which arguments."""
    calls = {}
    monkeypatch.setattr(cli, "configure_logging", lambda: "/dev/null")
    monkeypatch.setattr(cli, "run_sync", lambda **kw: calls.update(
        sync=kw) or 0)
    monkeypatch.setattr(cli, "run_archive", lambda **kw: calls.update(
        archive=kw) or 0)

    # Record the lock instead of taking it: these tests must never create a
    # .gitsync.lock in the real backup folder (or in a --backup-dir like
    # /tmp/mirrors). The real lock is tested in test_runlock.py.
    @contextlib.contextmanager
    def fake_lock(backup_dir, what):
        calls.setdefault("locks", []).append((backup_dir, what))
        yield

    monkeypatch.setattr(cli, "exclusive_run", fake_lock)
    return calls


def test_no_arguments_still_means_sync(dispatch):
    assert cli.main([]) == 0
    assert dispatch["sync"] == {"workers_override": None,
                                "cpu_load_override": None}


def test_sync_flags(dispatch):
    cli.main(["sync", "--workers", "12", "--cpu-load", "40"])
    assert dispatch["sync"] == {"workers_override": 12, "cpu_load_override": 40}


def test_archive_defaults(dispatch):
    cli.main(["archive"])
    assert dispatch["archive"]["mode"] == "full"
    assert dispatch["archive"]["compression"] == 6
    assert dispatch["archive"]["output"] is None
    assert dispatch["archive"]["excludes"] == []


def test_archive_flags(dispatch):
    cli.main(["archive", "--mode", "code", "--compression", "9",
              "-o", "/tmp/x.zip", "--backup-dir", "/tmp/mirrors",
              "--exclude", "*.pdf", "--exclude", "fixtures"])
    assert dispatch["archive"] == {
        "mode": "code", "compression": 9, "output": "/tmp/x.zip",
        "backup_dir": "/tmp/mirrors", "excludes": ["*.pdf", "fixtures"]}


@pytest.mark.parametrize("argv", [
    ["archive", "--mode", "everything"],
    ["archive", "--compression", "11"],
    ["nonsense"],
])
def test_bad_arguments_are_rejected(dispatch, argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2


def test_exit_code_comes_from_the_command(monkeypatch, dispatch):
    monkeypatch.setattr(cli, "run_sync", lambda **kw: 1)
    assert cli.main([]) == 1


def test_keyboard_interrupt_is_not_a_crash(monkeypatch, dispatch):
    def interrupt(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_sync", interrupt)
    assert cli.main([]) == 130


def test_an_unexpected_crash_is_logged_not_raised(monkeypatch, dispatch,
                                                  captured_log):
    def boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_sync", boom)
    assert cli.main([]) == 1
    assert any("kaboom" in line for line in captured_log)


def test_sigterm_goes_down_the_same_path_as_ctrl_c(monkeypatch, dispatch):
    # `docker stop` sends SIGTERM. Left alone it kills the process outright,
    # abandoning workers mid-clone and leaving a half-written zip behind.
    def stop_me(**kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.2)   # give the handler a chance to fire
        return 0

    monkeypatch.setattr(cli, "run_sync", stop_me)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert cli.main([]) == 130
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_system_exit_propagates(monkeypatch, dispatch):
    # Missing credentials must stop the run, not be swallowed as an error
    def missing(**kwargs):
        raise SystemExit(1)

    monkeypatch.setattr(cli, "run_sync", missing)
    with pytest.raises(SystemExit):
        cli.main([])


# --------------------------------------------------------------------------- #
# archive refreshes from GitHub first                                         #
# --------------------------------------------------------------------------- #

def test_archive_fetches_first_without_gitlab(dispatch):
    assert cli.main(["archive", "--backup-dir", "/tmp/mirrors"]) == 0
    assert dispatch["sync"]["push"] is False          # GitHub only
    assert dispatch["sync"]["backup_dir"] == "/tmp/mirrors"
    assert "archive" in dispatch


def test_archive_no_fetch_skips_github(dispatch):
    assert cli.main(["archive", "--no-fetch"]) == 0
    assert "sync" not in dispatch
    assert "archive" in dispatch


def test_a_failed_fetch_still_archives_but_fails_the_run(monkeypatch, dispatch):
    monkeypatch.setattr(cli, "run_sync", lambda **kw: 1)
    assert cli.main(["archive"]) == 1
    assert "archive" in dispatch    # the mirrors on disk still got zipped


def test_an_interrupted_fetch_archives_nothing(monkeypatch, dispatch):
    monkeypatch.setattr(cli, "run_sync", lambda **kw: 130)
    assert cli.main(["archive"]) == 130
    assert "archive" not in dispatch


def test_a_failed_archive_fails_the_run(monkeypatch, dispatch):
    monkeypatch.setattr(cli, "run_archive", lambda **kw: 1)
    assert cli.main(["archive"]) == 1


# --------------------------------------------------------------------------- #
# schedule: what gets repeated                                                 #
# --------------------------------------------------------------------------- #

@pytest.fixture
def scheduled(monkeypatch):
    """Capture what run_schedule was asked to do, without running the loop."""
    seen = {}
    monkeypatch.setattr(cli, "configure_logging", lambda: "/dev/null")

    def fake_run_schedule(expression, description, runner, run_on_start):
        seen.update(expression=expression, description=description,
                    runner=runner, run_on_start=run_on_start)
        return 0

    monkeypatch.setattr(cli, "run_schedule", fake_run_schedule)
    return seen


@pytest.mark.parametrize("argv, description", [
    (["schedule", "--cron", "@daily"], "sync"),
    (["schedule", "--cron", "@daily", "archive", "--mode", "code"], "archive --mode code"),
    (["schedule", "--cron", "@daily", "--", "archive", "--mode", "code"], "archive --mode code"),
])
def test_schedule_repeats_the_given_command(scheduled, argv, description):
    assert cli.main(argv) == 0
    assert scheduled["expression"] == "@daily"
    assert scheduled["description"] == description


def test_each_slot_runs_the_command_like_a_manual_run(scheduled, dispatch):
    cli.main(["schedule", "--cron", "@daily", "--run-on-start",
              "archive", "--mode", "code", "--no-fetch"])
    assert scheduled["run_on_start"] is True
    scheduled["runner"]()                       # what one slot does
    assert dispatch["archive"]["mode"] == "code"
    assert "sync" not in dispatch               # --no-fetch honoured


def test_a_schedule_inside_a_schedule_is_refused_up_front(scheduled):
    assert cli.main(["schedule", "--cron", "@daily",
                     "schedule", "--cron", "@hourly"]) == 2
    assert scheduled == {}                      # never started


def test_a_typo_in_the_scheduled_command_is_refused_up_front(scheduled):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["schedule", "--cron", "@daily", "archive", "--mode", "everything"])
    assert excinfo.value.code == 2
    assert scheduled == {}


def test_a_bad_cron_exits_2(monkeypatch):
    monkeypatch.setattr(cli, "configure_logging", lambda: "/dev/null")
    assert cli.main(["schedule", "--cron", "*/5 * * * *"]) == 2
    assert cli.main(["schedule", "--cron", "not cron"]) == 2


def test_python_dash_m_gitsync_works():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-m", "gitsync", "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "archive" in result.stdout


def test_each_scheduled_run_starts_a_new_log_file(scheduled, dispatch, monkeypatch):
    started = []
    monkeypatch.setattr(cli, "start_new_log_file", lambda: started.append(1))
    cli.main(["schedule", "--cron", "@daily", "sync"])
    scheduled["runner"]()
    scheduled["runner"]()
    assert started == [1, 1]
    assert "sync" in dispatch



# --------------------------------------------------------------------------- #
# One run at a time                                                            #
# --------------------------------------------------------------------------- #

def test_sync_and_archive_run_under_the_backup_folder_lock(dispatch, monkeypatch):
    monkeypatch.setattr(cli.config, "BACKUP_DIR", "/data/repos-backup")
    cli.main(["sync"])
    cli.main(["archive", "--mode", "code", "--backup-dir", "/elsewhere"])
    assert dispatch["locks"] == [("/data/repos-backup", "sync"),
                                 ("/elsewhere", "archive --mode code")]


def test_a_busy_backup_folder_skips_the_run(dispatch, monkeypatch, captured_log):
    @contextlib.contextmanager
    def busy(backup_dir, what):
        raise cli.RunInProgress("sync (pid 42, started 2026-10-03 02:00:00)")
        yield

    monkeypatch.setattr(cli, "exclusive_run", busy)
    assert cli.main(["sync"]) == cli.EXIT_BUSY == 75
    assert "sync" not in dispatch                      # never started
    assert any("Another GitSync run is still going (sync (pid 42"
               in line for line in captured_log)
