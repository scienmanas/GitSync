"""The archive command: roll the local mirrors into a single zip file.

The CLI refreshes the mirrors from GitHub first (a fetch-only sync, no GitLab
involved), so the zip is as current as the account; --no-fetch skips that and
zips whatever is already on disk.

Two flavours - 'full' keeps the complete git mirror (all history, plus whatever
was committed, vendored dependencies included), 'code' keeps only a snapshot of
the latest commit on each repo's default branch, with dependency and build
folders stripped out. Which folders count as "dependency and build" is
CODE_ARCHIVE_EXCLUDE_DIRS in .env, falling back to DEFAULT_DEPENDENCY_DIRS below
when that is left unset.
"""

import logging
import os
import subprocess
import tarfile
import tempfile
import time
import zipfile
from fnmatch import fnmatch
from pathlib import PurePosixPath

from rich.markup import escape

from . import config
from .logging_setup import console, log_file_path, set_current_repo
from .redaction import redact
from .ui import make_progress

logger = logging.getLogger(__name__)

# Folders that are dependencies/build output rather than code. Only stripped in
# "code" mode - "full" mode keeps whatever was committed. Used unless the user
# overrides the list with CODE_ARCHIVE_EXCLUDE_DIRS in .env - e.g. to keep
# `dist` around, set that variable to the default list with `dist` left out.

DEFAULT_DEPENDENCY_DIRS = frozenset({
    "node_modules", "bower_components", "jspm_packages", "vendor", "Pods",
    ".venv", "venv", "env", "virtualenv", "site-packages", "__pypackages__",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".gradle", ".terraform", ".next", ".nuxt", ".cache", ".parcel-cache",
    "dist", "build", "target", "out", "coverage",
})

DEPENDENCY_SUFFIXES = (".pyc", ".pyo", ".class")

# The active set of dependency folder names: whatever CODE_ARCHIVE_EXCLUDE_DIRS
# is set to, or DEFAULT_DEPENDENCY_DIRS when it is left unset.


def dependency_dirs():
    if config.CODE_ARCHIVE_EXCLUDE_DIRS:
        return frozenset(config.CODE_ARCHIVE_EXCLUDE_DIRS)
    return DEFAULT_DEPENDENCY_DIRS

# Should this path be left out of a "code only" archive?


def is_dependency_path(path, extra_excludes=()) -> bool:
    parts = PurePosixPath(path).parts
    # Folder names only (parts[:-1]): a *file* called `build` or `env` is
    # source, not a dependency folder. --exclude globs still see every part.
    if any(part in dependency_dirs() for part in parts[:-1]):
        return True
    if path.endswith(DEPENDENCY_SUFFIXES):
        return True
    for pattern in extra_excludes:
        if fnmatch(path, pattern) or any(fnmatch(part, pattern) for part in parts):
            return True
    return False

# Find the mirrors produced by a sync


def find_mirrors(backup_dir):
    if not os.path.isdir(backup_dir):
        return []
    mirrors = []
    for entry in sorted(os.listdir(backup_dir)):
        path = os.path.join(backup_dir, entry)
        if entry.endswith(".git") and os.path.isdir(path):
            mirrors.append((entry[:-len(".git")], path))
    return mirrors

# Human readable byte count


def human_size(num_bytes) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

# Does the mirror have anything checked in?


def has_commits(git_dir) -> bool:
    result = subprocess.run(
        ["git", "--git-dir", git_dir, "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True)
    return result.returncode == 0

# Whole mirror: full history, every branch and tag, restorable with `git clone`


def add_full_mirror(zf, git_dir, repo_name, compresslevel) -> int:
    added = 0
    # os.walk visits every folder under git_dir, top first, recursing on its
    # own, and yields (root, dirs, files) for each: that folder's path, its
    # subfolder names (unused - hence `_`) and its file names. It is a
    # generator, so the tree is walked lazily, one folder per iteration.
    for root, _, files in os.walk(git_dir):
        # Where this folder goes inside the zip. relpath strips the absolute
        # part (".../repo-a.git/objects/pack" -> "objects/pack"), and zip paths
        # always use "/", even on Windows where os.sep is "\".
        relative_root = os.path.relpath(root, git_dir).replace(os.sep, "/")
        # relpath of the top folder to itself is ".", which would otherwise
        # give entries like "repo-a.git/./HEAD" - so the top folder is plain
        # "repo-a.git/" and everything below it "repo-a.git/<path>/".
        prefix = f"{repo_name}.git/" if relative_root == "." else f"{repo_name}.git/{relative_root}/"

        # Store the folder itself, not just the files in it. A zip only holds
        # entries, so an empty folder vanishes unless it has one of its own -
        # and git refuses to open a repo whose empty folders (refs/heads/,
        # refs/tags/, objects/info/, ...) did not survive the round trip.
        # A name ending in "/" is what makes an entry a folder.
        directory = zipfile.ZipInfo(prefix)
        # external_attr is 32 bits: the top 16 hold the Unix mode, the bottom
        # 16 the MS-DOS/Windows attributes - set both so every unzip tool sees
        # a folder. 0o40755 = 0o040000 (S_IFDIR, "is a directory") + 0o755
        # (rwxr-xr-x; without x a folder cannot be entered), shifted into the
        # top half; 0x10 is Windows' FILE_ATTRIBUTE_DIRECTORY flag.
        # (zf.mkdir(prefix) does the same since Python 3.11.)
        directory.external_attr = (0o40755 << 16) | 0x10
        zf.writestr(directory, b"")   # a folder has no content

        for file_name in files:
            full_path = os.path.join(root, file_name)
            # A plain mirror has neither, but a symlink would be followed by
            # zf.write - possibly to somewhere outside the repo - and a socket
            # or fifo cannot be read like a file
            if os.path.islink(full_path) or not os.path.isfile(full_path):
                continue
            # zf.write reads the file from disk and takes its date and
            # permissions from it, plus the zip's own compression type - unlike
            # the hand-built ZipInfo entries in add_code_snapshot, nothing has
            # to be set by hand here
            zf.write(full_path, f"{prefix}{file_name}",
                     compresslevel=compresslevel)
            added += 1
    return added

# Latest commit only, with dependency/build folders stripped out


def add_code_snapshot(zf, git_dir, repo_name, excludes, compress_type, compresslevel):
    # A repo created on GitHub but never pushed to has no HEAD commit, and
    # `git archive HEAD` would fail on it. There is genuinely no code to keep,
    # so it is skipped with a warning rather than counted as a failure.
    if not has_commits(git_dir):
        logger.warning(
            "Skipping %s - the mirror has no commits yet", repo_name)
        return 0, 0

    added = 0
    skipped = 0
    with tempfile.TemporaryFile() as err_file:
        # A mirror is a bare repo - git's object database, no checked-out
        # files - so the code has to be pulled out of it somehow. Streaming a
        # tar straight out of `git archive` beats the alternatives: checking
        # out to a temp folder would write every file (node_modules included)
        # to disk just to read it back and delete it, and `git archive
        # --format=zip` would make one zip per repo with only git's pathspecs
        # for filtering. Here nothing touches the disk but the final zip,
        # skipped files are never written anywhere, and only what is committed
        # at HEAD (the default branch) is included.
        #
        # The command: --git-dir names the repo directly (a bare mirror has no
        # working folder to cd into), `archive` packs the files of one commit
        # straight from git's database, --format=tar because a tar can be read
        # front to back as a stream (a zip keeps its index at the end), and
        # HEAD - the default branch in a mirror - is the commit. With no output
        # file given, git writes the tar to stdout.
        #
        # Popen, not subprocess.run: it returns at once while git keeps going,
        # so the tar is read *as git writes it* instead of arriving as one
        # whole-repo blob in memory. Nor process.run, which suits clone/fetch
        # but here would log binary tar lines, fold stderr into the tar and
        # decode it all as text.
        #
        # stdout=PIPE: the tar flows into a pipe Python reads (proc.stdout)
        # rather than onto the terminal. The pipe also paces both sides - when
        # Python is busy compressing, it fills and git simply waits.
        #
        # stderr=err_file, not a second pipe: nobody reads stderr while the tar
        # is streaming, and an unread pipe holds only ~64 KB - a chatty git
        # would block writing to it while we block reading stdout, a deadlock.
        # A temp file never fills up; it is only read if git fails.
        proc = subprocess.Popen(
            ["git", "--git-dir", git_dir, "archive", "--format=tar", "HEAD"],
            stdout=subprocess.PIPE, stderr=err_file)
        try:
            assert proc.stdout is not None
            # "r|" reads the tar as a one-way stream - one member at a time,
            # never seeking back, which is all a pipe allows (plain "r" would
            # try to seek and fail) - so only the current file is held in
            # memory. A tar is simply [header][data][header][data]...
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                # Each iteration reads the next header and hands back a
                # TarInfo: the file's metadata only - name ("src/app.py"),
                # size, mode (0o100644), mtime (commit time) and type - not its
                # content
                for member in tar:
                    # Folders, symlinks and other special entries are not
                    # source files - a symlink survives only in full mode
                    if not member.isfile():
                        continue
                    # node_modules/..., *.pyc, --exclude matches. Skipping is
                    # nearly free: on the next iteration tarfile reads past
                    # this member's bytes in the pipe and throws them away
                    if is_dependency_path(member.name, excludes):
                        skipped += 1
                        continue
                    # A file-like view of this member's content, i.e. the bytes
                    # right after its header. In stream mode it has to be read
                    # *now*, before the loop moves on - once the next header is
                    # read, these bytes have gone past for good. None is only
                    # returned for non-regular files, which isfile() already
                    # excluded; the check is a safety net.
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    # There is no file on disk, only bytes, so zf.write (which
                    # takes a path) is out and writestr it is. Given just a
                    # name, writestr would stamp every file with *now* and
                    # rw------- permissions; building the ZipInfo ourselves
                    # keeps the commit's date and the real mode (a script's
                    # executable bit included) from the tar entry.
                    #
                    # mtime is a Unix timestamp; localtime turns it into a date
                    # (zip stores local time, with no timezone) and [:6] keeps
                    # the (year, month, day, hour, minute, second) ZipInfo
                    # wants, dropping weekday and the rest. The zip date format
                    # starts in 1980 and ZipInfo raises on anything older, so an
                    # ancient or bogus commit date is pinned to 1 Jan 1980.
                    date_time = time.localtime(member.mtime)[:6]
                    if date_time[0] < 1980:  # zip cannot represent older dates
                        date_time = (1980, 1, 1, 0, 0, 0)
                    # Each repo's code gets its own top-level folder in the
                    # zip: "repo-a/" + the path inside the repo
                    info = zipfile.ZipInfo(
                        f"{repo_name}/{member.name}", date_time=date_time)
                    # The catch: a ZipInfo we hand over is used exactly as
                    # given - zipfile only fills in its own defaults for the
                    # ZipInfo it builds itself - and a fresh one says
                    # ZIP_STORED. Without this line every code-mode file would
                    # be stored uncompressed, whatever --compression says.
                    info.compress_type = compress_type
                    # member.mode is e.g. 0o100755: 100 = regular file, 755 =
                    # rwxr-xr-x. & 0o7777 keeps only the permission bits and
                    # << 16 moves them into the Unix half of external_attr (see
                    # add_full_mirror for the layout) - which is how build.sh
                    # is still executable after unzipping.
                    info.external_attr = (member.mode & 0o7777) << 16
                    # read() pulls this file's bytes off the pipe and writestr
                    # compresses them and appends the entry; then it is on to
                    # the next header. It holds this one file in memory - fine
                    # for source, though a single multi-GB committed file would
                    # need as much RAM for a moment.
                    zf.writestr(info, extracted.read(),
                                compresslevel=compresslevel)
                    added += 1
        finally:
            # Whatever happened above - even a failure halfway (say, a full
            # disk while writing the zip) - stop git and reap it. Closing the
            # pipe makes a git still writing give up, and wait() collects its
            # exit status, so no zombie process is left and returncode below
            # is filled in.
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait()

        # Raise rather than just log, so run_archive counts the repo as failed
        # instead of passing a truncated snapshot off as a good one
        if proc.returncode != 0:
            err_file.seek(0)
            raise RuntimeError("git archive failed: " + redact(
                err_file.read().decode("utf-8", "replace").strip()))

    return added, skipped

# Bundle every mirrored repo into a single zip


def run_archive(mode, compression, output, backup_dir, excludes) -> int:
    mirrors = find_mirrors(backup_dir)
    if not mirrors:
        logger.error("No mirrored repositories found in %s", backup_dir)
        console.print(
            f"[red]No mirrored repositories found in {escape(backup_dir)}.[/red] "
            "Run [bold]python main.py sync[/bold] first.")
        return 1

    # Level 0 means "just bundle them", anything else deflates
    if compression == 0:
        compress_type = zipfile.ZIP_STORED
        compresslevel = None
    else:
        compress_type = zipfile.ZIP_DEFLATED
        compresslevel = compression

    if not output:
        output = os.path.join(
            config.ARCHIVE_DIR,
            f"gitsync-{mode}-{time.strftime('%Y%m%d_%H%M%S')}.zip")
    output = os.path.abspath(output)
    os.makedirs(os.path.dirname(output), exist_ok=True)

    logger.info("Archiving %d repos from %s (mode=%s, compression=%d) -> %s",
                len(mirrors), backup_dir, mode, compression, output)
    console.print(
        f"📦 Archiving [bold]{len(mirrors)}[/bold] repos in [bold]{mode}[/bold] mode "
        f"(compression {compression}) -> {escape(output)}")

    total_files = 0
    total_skipped = 0
    archived_repos = 0
    failed_repos = []

    progress = make_progress()
    task = progress.add_task(description="📦 Archiving", total=len(mirrors))

    try:
        with progress:
            # As one zip only
            with zipfile.ZipFile(output, "w", compression=compress_type,
                                 compresslevel=compresslevel, allowZip64=True) as zf:
                for repo_name, git_dir in mirrors:
                    progress.update(
                        task, description=f"📦 Archiving {escape(repo_name)}")
                    set_current_repo(repo_name)
                    try:
                        if mode == "full":
                            added = add_full_mirror(
                                zf, git_dir, repo_name, compresslevel)
                            skipped = 0
                        else:
                            added, skipped = add_code_snapshot(
                                zf, git_dir, repo_name, excludes, compress_type,
                                compresslevel)
                        total_files += added
                        total_skipped += skipped
                        if added:
                            archived_repos += 1
                        logger.info("Added %s (%d files, %d skipped)",
                                    repo_name, added, skipped)
                    except Exception as e:
                        failed_repos.append(repo_name)
                        logger.exception(
                            "Failed to archive %s: %s", repo_name, e)
                    finally:
                        set_current_repo(None)
                        progress.advance(task, 1)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user - removing the partial archive.")
        if os.path.exists(output):
            os.remove(output)
        console.print("[yellow]Interrupted - partial archive removed.[/yellow]")
        return 130

    size = os.path.getsize(output)
    summary = (f"{human_size(size)}, {archived_repos} repos, {total_files} files"
               + (f", {total_skipped} dependency files skipped" if mode == "code" else ""))
    # A zip missing repos is kept - most of a backup beats none - but it must
    # not report success, or a scheduled run would hide the gap for good
    if failed_repos:
        logger.error("Archive written with %d repo(s) missing: %s (%s) -> %s",
                     len(failed_repos), ", ".join(failed_repos), summary, output)
        console.print(
            f"[yellow]⚠️ Archive written, but {len(failed_repos)} repo(s) are missing:"
            f"[/yellow] {escape(', '.join(failed_repos))}\n   {escape(output)} "
            f"({summary}). Logs: {log_file_path()}")
        return 1
    logger.info("✅ Archive written: %s (%s)", output, summary)
    console.print(f"✅ Archive ready: [green]{escape(output)}[/green] ({summary})")
    return 0
