"""The live dashboard and the end-of-run summary.

The dashboard must never be taller than the terminal: a render that overflows
the screen makes rich repaint everything (flicker) and leaves a half-drawn box
behind when it stops. So its height depends only on the number of workers and
the "recently finished" rows - never on git's output - and no row ever wraps.
"""

import io

import pytest
from rich.console import Console

from gitsync import ui


class FakeSize:
    def __init__(self, height, width=80) -> None:
        self.height = height
        self.width = width


@pytest.fixture
def screen(monkeypatch):
    """Pretend the terminal is ROWS x 80, and render into a console that size."""

    def make(rows):
        monkeypatch.setattr(type(ui.console), "size",
                            property(lambda self: FakeSize(rows)))
        return Console(file=io.StringIO(), width=80, height=rows,
                       force_terminal=True, color_system=None)

    return make


def rendered_lines(console, renderable):
    console.print(renderable)
    return console.file.getvalue().rstrip("\n").split("\n")


def busy_board(workers=16, finished=12):
    board = ui.ActiveRepos()
    for i in range(finished):
        name = f"finished-repo-{i}-" + "x" * 120          # far wider than the screen
        board.add(name)
        if i % 3:
            board.finish(name, True)
        else:
            board.finish(name, False, "push",
                         "remote: GitLab: You are not allowed to force push code "
                         "to a protected branch on this project. " * 3)
    for i in range(workers):
        name = f"working-repo-{i}-" + "y" * 120
        board.add(name)
        board.step(name, "↑ pushing to GitLab")
    return board


@pytest.mark.parametrize("rows", [8, 12, 24, 50])
def test_the_dashboard_never_outgrows_the_terminal(screen, rows):
    console = screen(rows)
    dashboard = ui.SyncDashboard(ui.make_progress(), busy_board(), total=40)
    lines = rendered_lines(console, dashboard)
    assert len(lines) < rows                       # always one row to spare
    assert all(len(line) <= 80 for line in lines)  # cut, never wrapped


def test_more_workers_than_fit_are_summed_up(screen):
    console = screen(14)
    text = "\n".join(rendered_lines(
        console, ui.SyncDashboard(ui.make_progress(), busy_board(workers=16), 40)))
    assert "more" in text                          # "+N more" instead of overflowing


def test_the_dashboard_shows_steps_counts_and_reasons(screen):
    console = screen(40)
    board = ui.ActiveRepos()
    board.add("dotfiles")
    board.step("dotfiles", "↓ cloning from GitHub")
    board.add("Bussie")
    board.finish("Bussie", False, "push", "protected branch")
    board.add("api")
    board.finish("api", True)
    text = "\n".join(rendered_lines(
        console, ui.SyncDashboard(ui.make_progress(), board, total=10)))
    assert "dotfiles" in text and "cloning from GitHub" in text
    assert "✔ 1 synced" in text and "✖ 1 failed" in text and "8 left" in text
    assert "push: protected branch" in text
    # newest first in "Recently finished"
    assert text.index("api") < text.index("Bussie")


def test_the_board_counts_and_keeps_the_failures():
    board = ui.ActiveRepos()
    for name in ("a", "b", "c"):
        board.add(name)
    board.finish("a", True)
    board.finish("b", False, "fetch", "fatal: repository not found")
    board.finish("c", False)                     # no reason given
    assert (board.succeeded, board.failed) == (1, 2)
    assert board.failures == [("b", "fetch", "fatal: repository not found"),
                              ("c", "?", "see the log")]
    assert board.describe() == "waiting..."      # all finished


def test_describe_still_names_the_busy_repos():
    board = ui.ActiveRepos()
    for name in ("a", "b", "c", "d", "e"):
        board.add(name)
    assert board.describe() == "a, b, c (+2 more)"


def test_the_summary_names_every_failure(monkeypatch):
    out = Console(file=io.StringIO(), width=120, color_system=None)
    monkeypatch.setattr(ui, "console", out)
    board = ui.ActiveRepos()
    board.add("api")
    board.finish("api", True)
    board.add("Bussie")
    board.finish("Bussie", False, "push", "remote: GitLab: You are not allowed "
                                          "to force push code to a protected branch")
    ui.print_summary(board, "synced", took=139, skipped_forks=21,
                     log_path="/tmp/logs/logs_x.txt")
    text = out.file.getvalue()
    assert "finished in 2m 19s - 1 synced, 1 failed, 21 fork(s) skipped" in text
    assert "Failed (1):" in text
    assert "Bussie" in text and "push" in text and "protected branch" in text
    assert "Full log: /tmp/logs/logs_x.txt" in text


def test_a_clean_run_has_no_failure_list(monkeypatch):
    out = Console(file=io.StringIO(), width=120, color_system=None)
    monkeypatch.setattr(ui, "console", out)
    board = ui.ActiveRepos()
    board.add("api")
    board.finish("api", True)
    ui.print_summary(board, "fetched", took=5)
    text = out.file.getvalue()
    assert "Fetch finished in 5s - 1 fetched, 0 failed" in text
    assert "Failed" not in text


@pytest.mark.parametrize("seconds, expected", [
    (0, "0s"), (59, "59s"), (60, "1m 00s"), (139, "2m 19s"), (3600, "1h 00m"),
    (5400, "1h 30m"),
])
def test_format_duration(seconds, expected):
    assert ui.format_duration(seconds) == expected



LONG_REASON = ("remote: GitLab: You are not allowed to force push code to a protected "
               "branch on this project. - set GITLAB_ALLOW_FORCE_PUSH=true to let "
               "GitSync allow it")
LONG_LOG = "/data/logs/" + "nested/" * 12 + "logs_20261003_160231.txt"


def failed_board():
    board = ui.ActiveRepos()
    board.add("Bussie")
    board.finish("Bussie", False, "push", LONG_REASON)
    return board


def test_without_a_terminal_every_summary_line_stays_whole(monkeypatch):
    # docker logs / cron: rich assumes 80 columns - nothing may be folded
    out = Console(file=io.StringIO(), width=80, color_system=None)   # not a tty
    monkeypatch.setattr(ui, "console", out)
    ui.print_summary(failed_board(), "synced", took=5, log_path=LONG_LOG)
    lines = out.file.getvalue().splitlines()
    assert f"  Bussie - push: {LONG_REASON}" in lines        # one line, all of it
    assert f"Full log: {LONG_LOG}" in lines                   # the path in one piece
    assert not any(line != line.rstrip() for line in lines)   # no padding


def test_on_a_terminal_failures_are_an_aligned_table(monkeypatch):
    out = Console(file=io.StringIO(), width=80, force_terminal=True, color_system=None)
    monkeypatch.setattr(ui, "console", out)
    ui.print_summary(failed_board(), "synced", took=5)
    text = out.file.getvalue()
    assert "Bussie  push  remote: GitLab:" in text            # columns, not "name - step:"
    assert all(len(line) <= 80 for line in text.splitlines())  # folded to fit the screen
