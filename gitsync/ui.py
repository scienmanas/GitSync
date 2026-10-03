"""The rich widgets shared by the sync and the archive commands."""

import threading
import time
from collections import deque

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table
from rich.text import Text

from .logging_setup import console

# How many finished repos the "Recently finished" box lists
RECENT_ROWS = 8

# Rows the dashboard needs besides the repo rows: the progress bar, the counts
# line, and the top and bottom border of each of the two boxes - plus one spare,
# so the render never exactly fills the screen.
DASHBOARD_CHROME_ROWS = 7

# The usual bar: description, bar, "n / total", elapsed and time left


def make_progress() -> Progress:
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),      # shows "n / total"
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        expand=True,
        console=console,
        # A repainting bar is noise in a docker/cron log, which is a plain file
        # with no cursor to move - the log lines carry the progress there.
        disable=not console.is_terminal
    )


def format_duration(seconds) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"

# What every worker is doing right now, what has finished, and why anything
# failed - written by the worker threads, read by the dashboard (4 times a
# second) and the end-of-run summary. One lock guards it all, since workers
# update it in parallel.


class ActiveRepos:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = {}            # repo -> [current step, when it started]
        self._recent = deque(maxlen=RECENT_ROWS)   # (repo, ok, detail), newest last
        self.failures = []           # (repo, step, reason)
        self.succeeded = 0
        self.failed = 0

    def add(self, repo_name) -> None:
        with self._lock:
            self._active[repo_name] = ["starting", time.monotonic()]

    def step(self, repo_name, label) -> None:
        with self._lock:
            if repo_name in self._active:
                self._active[repo_name][0] = label

    # Record how a repo ended. `step` and `reason` say where and why it failed.
    def finish(self, repo_name, ok, step=None, reason=None) -> None:
        with self._lock:
            entry = self._active.pop(repo_name, None)
            took = format_duration(time.monotonic() - entry[1]) if entry else ""
            if ok:
                self.succeeded += 1
                self._recent.append((repo_name, True, took))
            else:
                self.failed += 1
                reason = reason or "see the log"
                self.failures.append((repo_name, step or "?", reason))
                self._recent.append((repo_name, False, f"{step or '?'}: {reason}"))

    # Forget a repo without recording a result (an unexpected crash path - the
    # repo is still counted by whoever reports it)
    def remove(self, repo_name) -> None:
        with self._lock:
            self._active.pop(repo_name, None)

    def describe(self) -> str:
        with self._lock:
            active = list(self._active)
        if not active:
            return "waiting..."
        shown = ", ".join(active[:3])
        if len(active) > 3:
            shown += f" (+{len(active) - 3} more)"
        return shown

    # A consistent copy for drawing, taken under the lock
    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            active = [(name, step, format_duration(now - started))
                      for name, (step, started) in self._active.items()]
            return active, list(self._recent), self.succeeded, self.failed


# The live view: progress bar, counts, what each worker is doing, and the last
# few results. Its height is fixed by the number of workers and RECENT_ROWS -
# never by how much git prints - and every row is cut to one line instead of
# wrapping, so it always fits the screen. A render taller than the screen is
# what makes rich repaint everything (flicker) and leaves a half-drawn box
# behind when it stops. Raw git output goes to the log file only.


class SyncDashboard:
    def __init__(self, progress, board, total, verb="synced") -> None:
        self.progress = progress
        self.board = board
        self.total = total
        self.verb = verb

    def __rich__(self):
        active, recent, succeeded, failed = self.board.snapshot()
        left = self.total - succeeded - failed

        counts = Text("  ")
        counts.append(f"✔ {succeeded} {self.verb}", style="green")
        counts.append("   ")
        counts.append(f"✖ {failed} failed", style="red" if failed else "dim")
        counts.append("   ")
        counts.append(f"⏳ {left} left", style="dim")

        # Share out the screen's rows: the "Working on" box first, then
        # "Recently finished" gets what is left (and is dropped if nothing is),
        # so the whole thing stays inside the terminal however small it is.
        budget = max(1, console.size.height - DASHBOARD_CHROME_ROWS)
        working_lines = min(max(1, len(active)), budget)
        # When not every repo fits, the last line becomes "+N more" - it is a
        # row too, so one fewer repo is shown
        shown = (len(active) if len(active) <= working_lines
                 else working_lines - 1)
        recent_rows = min(RECENT_ROWS, budget - working_lines)

        working = Table.grid(expand=True, padding=(0, 2))
        working.add_column(ratio=3, no_wrap=True, overflow="ellipsis")
        working.add_column(ratio=3, no_wrap=True, overflow="ellipsis", style="cyan")
        working.add_column(justify="right", no_wrap=True, style="dim")
        for name, step, took in active[:shown]:
            working.add_row(escape(name), step, took)
        if len(active) > shown:
            working.add_row(f"[dim]+{len(active) - shown} more[/dim]", "", "")
        if not active:
            working.add_row("[dim]waiting...[/dim]", "", "")

        finished = Table.grid(expand=True, padding=(0, 2))
        finished.add_column(no_wrap=True, width=1)
        finished.add_column(ratio=3, no_wrap=True, overflow="ellipsis")
        finished.add_column(ratio=4, no_wrap=True, overflow="ellipsis")
        for name, ok, detail in list(reversed(recent))[:recent_rows]:
            finished.add_row("[green]✔[/green]" if ok else "[red]✖[/red]",
                             escape(name),
                             f"[dim]{escape(detail)}[/dim]" if ok
                             else f"[red]{escape(detail)}[/red]")
        if not recent:
            finished.add_row("", "[dim]nothing yet[/dim]", "")

        parts = [self.progress, counts,
                 Panel(working, title="Working on", title_align="left",
                       border_style="cyan")]
        if recent_rows:
            parts.append(Panel(finished, title="Recently finished",
                               title_align="left", border_style="green"))
        return Group(*parts)


# The end-of-run summary. Printed in every mode - in a terminal it is what stays
# on screen once the dashboard is gone, in docker/cron it is the last thing in
# the log - and it names every failed repo with the step and the reason.


def print_summary(board, verb, took, skipped_forks=0, log_path=None,
                  interrupted=False) -> None:
    title = "Interrupted" if interrupted else ("✅ " if not board.failed else "⚠️  ") + \
        ("Sync" if verb == "synced" else "Fetch") + " finished"
    line = Text()
    line.append(f"{title} in {format_duration(took)} - ", style="bold")
    line.append(f"{board.succeeded} {verb}", style="green")
    line.append(", ")
    line.append(f"{board.failed} failed", style="red" if board.failed else "")
    if skipped_forks:
        line.append(f", {skipped_forks} fork(s) skipped", style="dim")
    console.print()
    # soft_wrap: never fold a line. Without a terminal (docker logs, cron)
    # rich assumes 80 columns and would chop reasons and the log path into
    # pieces padded with spaces; one long line reads and greps far better.
    console.print(line, soft_wrap=True)

    if board.failures:
        console.print(f"\n[red]✖ Failed ({len(board.failures)}):[/red]")
        if console.is_terminal:
            # On screen: aligned columns, the reason folded under itself
            table = Table.grid(padding=(0, 2))
            table.add_column(style="bold", no_wrap=True)
            table.add_column(style="cyan", no_wrap=True)
            table.add_column(overflow="fold")
            for name, step, reason in board.failures:
                table.add_row("  " + escape(name), escape(step), escape(reason))
            console.print(table)
        else:
            # In a log: one whole line per failed repo
            for name, step, reason in board.failures:
                console.print(f"  {escape(name)} - {escape(step)}: {escape(reason)}",
                              soft_wrap=True, highlight=False)
    if log_path:
        console.print(f"\n[dim]Full log: {escape(log_path)}[/dim]", soft_wrap=True,
                      highlight=False)
    console.print()
