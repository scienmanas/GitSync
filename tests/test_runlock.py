"""One GitSync run at a time per backup folder."""

import subprocess
import sys
import time

import pytest

from gitsync.runlock import LOCK_NAME, RunInProgress, exclusive_run


def test_a_second_run_is_refused_while_the_first_holds_the_lock(tmp_path):
    with exclusive_run(str(tmp_path), "sync"):
        with pytest.raises(RunInProgress, match=r"sync \(pid \d+, started "):
            with exclusive_run(str(tmp_path), "archive --mode code"):
                pytest.fail("a second run got in")


def test_the_lock_is_free_again_afterwards(tmp_path):
    with exclusive_run(str(tmp_path), "sync"):
        pass
    with exclusive_run(str(tmp_path), "archive --mode full"):
        pass                                   # no RunInProgress


def test_the_lock_is_freed_when_the_run_fails(tmp_path):
    with pytest.raises(RuntimeError):
        with exclusive_run(str(tmp_path), "sync"):
            raise RuntimeError("boom")
    with exclusive_run(str(tmp_path), "sync"):
        pass


def test_different_backup_folders_do_not_block_each_other(tmp_path):
    with exclusive_run(str(tmp_path / "a"), "sync"):
        with exclusive_run(str(tmp_path / "b"), "sync"):
            pass


def test_a_killed_run_never_leaves_a_stale_lock(tmp_path):
    # Another process takes the lock, then dies without cleaning up (SIGKILL).
    # The kernel drops its flock, so the next run is not blocked forever.
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, sys.argv[2]);"
         "from gitsync.runlock import exclusive_run;"
         "cm = exclusive_run(sys.argv[1], 'sync'); cm.__enter__();"
         "print('held', flush=True); time.sleep(60)",
         str(tmp_path), str(__import__("pathlib").Path(__file__).parent.parent)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(RunInProgress):          # really held by the other process
            with exclusive_run(str(tmp_path), "sync"):
                pass
    finally:
        holder.kill()
        holder.wait()
    with exclusive_run(str(tmp_path), "sync"):     # free the moment it died
        pass


def test_the_lock_file_never_ends_up_in_an_archive(tmp_path):
    # find_mirrors only picks *.git folders; the lock is a plain file
    from gitsync.archive import find_mirrors
    with exclusive_run(str(tmp_path), "sync"):
        assert (tmp_path / LOCK_NAME).exists()
        assert find_mirrors(str(tmp_path)) == []
