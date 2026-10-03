"""The container packaging.

Nothing here builds an image - these are the checks that catch the mistakes
that survive a successful `docker build`: a shell typo in the entrypoint, the
image and the script disagreeing about where /data is, or a .env finding its
way into a layer.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
CI = ROOT / ".github/workflows/ci.yml"
PUBLISH = ROOT / ".github/workflows/publish-image.yml"


def test_entrypoint_is_valid_posix_sh():
    # Alpine runs it with busybox ash, not bash - `sh -n` parses without running
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True,
                   capture_output=True)


def test_entrypoint_is_executable():
    assert ENTRYPOINT.stat().st_mode & 0o111, "docker-entrypoint.sh is not +x"


@pytest.mark.parametrize("variable, path", [
    ("BACKUP_DIR", "/data/repos-backup"),
    ("ARCHIVE_DIR", "/data/archives"),
    ("LOGS_DIR", "/data/logs"),
])
def test_image_and_entrypoint_agree_on_the_data_paths(variable, path):
    # Both sides carry a default; if they drift, a container started with
    # --entrypoint writes its backup somewhere the volume does not cover.
    assert f"{variable}={path}" in DOCKERFILE.read_text()
    assert f'{variable}="${{{variable}:-{path}}}"' in ENTRYPOINT.read_text()


def test_everything_worth_keeping_is_under_the_volume():
    # One VOLUME line is what makes a restart a fetch instead of a re-clone
    assert 'VOLUME ["/data"]' in DOCKERFILE.read_text()


def test_the_image_runs_unprivileged():
    assert "USER gitsync" in DOCKERFILE.read_text()


@pytest.mark.parametrize("entry", [".env", ".git", "repos-backup", "Logs"])
def test_dockerignore_keeps_secrets_and_state_out_of_the_image(entry):
    # A .env copied into a layer is a token published with the image
    assert entry in DOCKERIGNORE.read_text().split()


# --------------------------------------------------------------------------- #
# Publishing                                                                   #
# --------------------------------------------------------------------------- #

def test_the_image_is_never_built_by_the_everyday_ci():
    # Every push and every dependabot PR would otherwise pay for a build that
    # proves something the packaging tests above already cover
    assert "docker build" not in CI.read_text()


def test_publishing_only_happens_when_asked():
    workflow = PUBLISH.read_text()
    triggers = workflow.split("jobs:")[0]
    assert "workflow_dispatch:" in triggers
    # No push/schedule trigger: an image goes out because someone decided so
    assert "\n  push:" not in triggers
    assert "schedule:" not in triggers


def test_publishing_only_happens_from_main():
    assert "if: github.ref == 'refs/heads/main'" in PUBLISH.read_text()


def test_the_version_reaches_the_image():
    assert "ARG VERSION" in DOCKERFILE.read_text()
    assert "org.opencontainers.image.version" in DOCKERFILE.read_text()
    assert "build-args: VERSION=" in PUBLISH.read_text()


def test_the_commit_is_always_tagged():
    # :latest and a version tag can both be moved; the sha tag is what lets you
    # work out which tree a running container came from
    assert "sha-$(git rev-parse --short HEAD)" in PUBLISH.read_text()


def shell_bodies():
    """Every `run:` script in the workflows, with the step name it belongs to."""
    for workflow in (CI, PUBLISH):
        lines = workflow.read_text().splitlines()
        name = "?"
        for index, line in enumerate(lines):
            if line.strip().startswith("- name:"):
                name = line.split("name:", 1)[1].strip()
            if not line.strip().startswith("run: |"):
                continue
            indent = len(line) - len(line.lstrip())
            body = []
            for following in lines[index + 1:]:
                # The block ends at the first line indented no further than `run:`
                if following.strip() and len(following) - len(following.lstrip()) <= indent:
                    break
                body.append(following)
            yield f"{workflow.name}: {name}", "\n".join(body)


def test_workflow_scripts_never_have_values_pasted_into_them():
    # ${{ }} is textual substitution done before bash starts, so an input
    # containing shell syntax would be executed. Values belong in env:, where
    # bash reads them back as data.
    for where, body in shell_bodies():
        assert "${{" not in body, f"{where} interpolates an expression into a shell script"


def test_the_version_input_is_validated_before_anything_is_built():
    # A tag docker will not accept should fail in seconds, not after a
    # multi-architecture build has run
    assert "is not a usable image tag" in PUBLISH.read_text()


def entrypoint_code():
    """The lines that actually run - comments explain crond, code must not use it."""
    return [line for line in ENTRYPOINT.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_the_schedule_is_kept_by_the_programme_not_by_crond():
    # busybox crond silently declines to start a job when it is not root, and
    # this image runs as uid 1000 - so the loop lives in main.py instead.
    assert any("main.py schedule --cron" in line for line in entrypoint_code())
    assert not any("crond" in line for line in entrypoint_code())


def test_flags_reach_the_cli():
    # `docker run gitsync --help` is the first thing anyone types, and without
    # this it would try to execute "--help" as a programme
    assert any(line.strip().startswith("-*)") for line in entrypoint_code())


def test_the_scheduler_is_pid_1():
    # `docker stop` signals PID 1; anything else and SIGTERM never reaches the
    # process that knows how to stop a sync tidily.
    assert "exec python /app/main.py schedule" in ENTRYPOINT.read_text()


ENV_EXAMPLE = ROOT / ".env.example"


def test_env_example_has_no_inline_comments():
    # `docker run --env-file` keeps a trailing "# ..." as part of the value
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            assert " #" not in line, line


@pytest.mark.parametrize("variable", ["BACKUP_DIR", "ARCHIVE_DIR", "LOGS_DIR"])
def test_env_example_leaves_the_data_paths_to_the_image(variable):
    # A copied .env setting BACKUP_DIR=./repos-backup would override /data and
    # put the backup outside the volume
    for line in ENV_EXAMPLE.read_text().splitlines():
        assert not line.startswith(f"{variable}="), line



def dockerfile_description():
    import re
    return re.search(r'org\.opencontainers\.image\.description="([^"]*)"',
                     DOCKERFILE.read_text()).group(1)


def test_the_image_description_fits_ghcr():
    # GHCR shows at most 512 characters on the package page
    assert 0 < len(dockerfile_description()) <= 512


def test_the_package_page_and_docker_inspect_show_the_same_description():
    import re
    workflow = PUBLISH.read_text()
    published = re.search(r"IMAGE_DESCRIPTION: '([^']*)'", workflow).group(1)
    assert published == dockerfile_description()
    # Multi-arch: GHCR only reads it from the index annotation, not a label
    assert ("index:org.opencontainers.image.description=${{ env.IMAGE_DESCRIPTION }}"
            in workflow)



SETUP = ROOT / "docker-setup.sh"


def test_docker_setup_is_valid_bash_and_executable():
    assert subprocess.run(["bash", "-n", str(SETUP)]).returncode == 0
    assert SETUP.stat().st_mode & 0o111


def test_docker_setup_never_puts_a_token_on_the_command_line():
    # docker run arguments show up in `ps` and shell history; tokens only
    # travel through --env-file
    script = SETUP.read_text()
    runs = [line for line in script.splitlines() if "docker run" in line]
    assert runs and all("TOKEN" not in line for line in runs)
    assert "--env-file .env" in script


def test_docker_setup_pins_the_data_paths_to_the_volume():
    # -e beats --env-file: a local BACKUP_DIR in .env must not move the backups
    assert "-e BACKUP_DIR=/data/repos-backup -e ARCHIVE_DIR=/data/archives " \
           "-e LOGS_DIR=/data/logs" in SETUP.read_text()


@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
def test_the_local_data_folder_is_never_committed_or_baked_in(ignore_file):
    assert "gitsync-data" in (ROOT / ignore_file).read_text()



# --------------------------------------------------------------------------- #
# docker-setup.sh, run for real against a stand-in `docker` (and `curl`)      #
# --------------------------------------------------------------------------- #

FAKE_DOCKER = """#!/bin/bash
# Records each call, and whether the token reached docker through its
# environment (what `-e NAME` without a value relies on)
echo "ARGS $*" >> "$CALLS"
echo "ENV_TOKEN ${GITHUB_TOKEN:-<none>}" >> "$CALLS"
case "$1" in
  version) echo 29 ;;
  ps) ;;                       # no containers yet
  *) exit 0 ;;                 # info, pull, the schedule check, run: all fine
esac
"""

# Offline by default: the registry can't be reached
FAKE_CURL_OFFLINE = "#!/bin/bash\nexit 7\n"

# The registry answering, the way ghcr.io does for a public package
FAKE_CURL_REGISTRY = """#!/bin/bash
case "$*" in
  *ghcr.io/token*) echo '{"token":"anon"}' ;;
  *tags/list*) echo '{"name":"scienmanas/gitsync","tags":["1.9.0","sha-6957340","2.0.0","latest","1.10.0"]}' ;;
  *) exit 7 ;;
esac
"""

TOKEN = "ghp_" + "x" * 36


def run_setup(tmp_path, answers, existing_env=None, curl=FAKE_CURL_OFFLINE,
              changelog=None):
    """Run docker-setup.sh in a copy of the project with scripted answers."""
    import os
    import shutil
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(SETUP, project / "docker-setup.sh")
    if existing_env is not None:
        (project / ".env").write_text(existing_env)
    if changelog is not None:
        (project / "CHANGELOG.md").write_text(changelog)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", curl)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls),
           "NO_COLOR": "1"}
    env.pop("GITHUB_TOKEN", None)
    result = subprocess.run(["bash", "docker-setup.sh"], cwd=project, env=env,
                            input="\n".join(answers) + "\n", text=True,
                            capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    log = calls.read_text().splitlines()
    started = [line for line in log if line.startswith("ARGS run -d")]
    seen = log[log.index(started[0]) + 1] if started else None
    return project, started, seen, result.stdout


# image (latest), GitHub user + token, skip GitLab, then the defaults - except
# a Docker volume (no --user) - and the final "save to .env?" answer
def answers(save):
    return ["1", "octocat", TOKEN, "", "", "", "", "", "", "2", "", save]


def test_saved_settings_go_through_env_file(tmp_path):
    project, started, _, _ = run_setup(tmp_path, answers(save=""))
    env_file = project / ".env"
    assert f"GITHUB_TOKEN={TOKEN}" in env_file.read_text()
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    assert "--env-file .env" in started[0]
    assert TOKEN not in started[0]
    assert started[0].endswith("ghcr.io/scienmanas/gitsync:latest")


def test_unsaved_settings_go_straight_to_docker_without_a_file(tmp_path):
    project, started, seen, _ = run_setup(tmp_path, answers(save="n"))
    assert not (project / ".env").exists()
    assert "--env-file" not in started[0]
    assert " -e GITHUB_TOKEN " in started[0] + " "      # the name only...
    assert TOKEN not in started[0]                       # ...never the value
    assert seen == f"ENV_TOKEN {TOKEN}"                  # docker still got it


def test_not_saving_leaves_an_existing_env_file_alone(tmp_path):
    old = "GITHUB_USER=someone\nGITHUB_TOKEN=old\n"
    # "n" = don't use .env's values, answer everything fresh, then don't save
    project, started, _, _ = run_setup(tmp_path, ["1", "n"] + answers(save="n")[1:],
                                       existing_env=old)
    assert (project / ".env").read_text() == old
    assert "--env-file" not in started[0]


EXISTING = """# my own notes
GITHUB_USER=someone
GITHUB_TOKEN=old-github-token
GITLAB_USER=labber
GITLAB_TOKEN=old-gitlab-token
CRON_SCHEDULE=0 5 * * *
FOO=bar
"""


def test_env_values_are_the_defaults_and_enter_keeps_them(tmp_path):
    keep_everything = ["1", "",                    # latest; use .env's values
                       "", "",                     # GitHub user, token: kept
                       "", "", "", "", "",         # GitLab user, token, group, visibility, force push
                       "", "",                     # sync (has GitLab), on a schedule
                       "", "", "",                 # schedule, timezone, run now: kept / defaults
                       "2", "", ""]                # volume, no extras, save
    project, started, _, out = run_setup(tmp_path, keep_everything, existing_env=EXISTING)
    saved = (project / ".env").read_text()
    for line in ("GITHUB_USER=someone", "GITHUB_TOKEN=old-github-token",
                 "GITLAB_TOKEN=old-gitlab-token", "CRON_SCHEDULE=0 5 * * *",
                 "CRON_COMMAND=sync", "GITLAB_ALLOW_FORCE_PUSH=true"):
        assert line in saved
    assert "# my own notes" in saved and "FOO=bar" in saved   # untouched
    assert "old-github-token" not in out                      # shown masked
    assert "••••••••••••••en" in out


def test_a_dash_removes_gitlab(tmp_path):
    answers_ = ["1", "", "", "", "-",              # keep GitHub, "-" stops using GitLab
                "", "", "", "", "", "2", "", ""]  # archive (no GitLab), schedule, ..., save
    project, _, _, _ = run_setup(tmp_path, answers_, existing_env=EXISTING)
    saved = (project / ".env").read_text()
    assert "GITLAB_USER" not in saved and "GITLAB_TOKEN" not in saved
    assert "CRON_COMMAND=archive --mode full" in saved


def test_another_tag_can_be_typed(tmp_path):
    # offline, no Dockerfile: the menu is "latest" and "Another tag..."
    project, started, _, _ = run_setup(tmp_path, ["2", "sha-6957340"] + answers("")[1:])
    assert started[0].endswith("ghcr.io/scienmanas/gitsync:sha-6957340")


def test_the_versions_on_the_registry_are_offered_newest_first(tmp_path):
    changelog = "## [2.0.0][2.0.0] - 2026-10-03\n\n## [1.10.0] - 2026-09-01\n"
    # 1) latest  2) 2.0.0  3) 1.10.0  4) 1.9.0  5) Another tag - pick 2.0.0
    project, started, _, out = run_setup(tmp_path, ["2"] + answers("")[1:],
                                         curl=FAKE_CURL_REGISTRY, changelog=changelog)
    menu = [line.strip() for line in out.splitlines() if line.strip()[:2] in
            ("1)", "2)", "3)", "4)", "5)")][:5]
    assert menu[0].startswith("1) latest")
    assert menu[1].startswith("2) 2.0.0") and "released 2026-10-03" in menu[1]
    assert menu[2].startswith("3) 1.10.0") and "released 2026-09-01" in menu[2]
    assert menu[3].startswith("4) 1.9.0")
    assert "sha-" not in " ".join(menu[:4])          # build tags aren't versions
    assert started[0].endswith("ghcr.io/scienmanas/gitsync:2.0.0")


def test_the_arrow_key_menu_works_in_a_real_terminal():
    # The menus fall back to typed numbers without a terminal, so the arrow-key
    # path is driven here through a pseudo-terminal, like a person would
    import os
    import pty
    import select
    import time
    script = SETUP.read_text()
    functions = script[script.index("say()"):script.index("# Set KEY=VALUE in .env")]
    program = (functions + '\nchoose PICK 1 "Pick one" "first" "second" "third"\n'
               'echo "PICKED=$PICK"\n')
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "-c", "BOLD= DIM= CYAN= RESET=\n" + program])
    out = b""

    def read_for(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    return

    read_for(0.5)
    for keys in (b"\x1b[B", b"\x1b[B", b"\x1b[A", b"\r"):   # down, down, up, Enter
        os.write(fd, keys)
        read_for(0.2)
    read_for(0.5)
    os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert "PICKED=2" in text                       # down, down, up = the second
    assert "\x1b[?25l" in text and "\x1b[?25h" in text   # cursor hidden, then back
