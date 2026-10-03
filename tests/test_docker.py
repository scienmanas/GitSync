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
