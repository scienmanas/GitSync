"""Settings that only matter once the programme is somewhere other than a
developer's checkout - a container, mostly.
"""

import importlib

import pytest

from gitsync import config


@pytest.fixture
def reload_config(monkeypatch):
    """Re-read the environment, then put the module back as it was."""
    def reload():
        return importlib.reload(config)

    yield reload
    monkeypatch.undo()
    importlib.reload(config)


def test_logs_dir_can_be_pointed_at_a_volume(reload_config, monkeypatch,
                                             tmp_path):
    # The default sits inside the image and disappears on every restart
    monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
    assert reload_config().LOGS_FOLDER == str(tmp_path / "logs")


def test_logs_default_next_to_the_project(reload_config, monkeypatch):
    monkeypatch.delenv("LOGS_DIR", raising=False)
    reloaded = reload_config()
    assert reloaded.LOGS_FOLDER == f"{reloaded.PROJECT_DIR}/Logs"


@pytest.mark.parametrize("variable, attribute, value", [
    ("BACKUP_DIR", "BACKUP_DIR", "/data/repos-backup"),
    ("ARCHIVE_DIR", "ARCHIVE_DIR", "/data/archives"),
    ("CPU_LOAD_PERCENT", "CPU_LOAD_PERCENT", "40"),
    ("MAX_WORKERS", "MAX_WORKERS", "3"),
    ("LOG_RETENTION_DAYS", "LOG_RETENTION_DAYS", "7"),
    ("GITLAB_ALLOW_FORCE_PUSH", "GITLAB_ALLOW_FORCE_PUSH", "True"),
])
def test_every_knob_is_reachable_from_the_environment(reload_config, monkeypatch,
                                                      variable, attribute, value):
    # A container is configured with -e and nothing else, so anything that is
    # only settable in .env would be unreachable there.
    monkeypatch.setenv(variable, value)
    got = getattr(reload_config(), attribute)
    assert str(got) == value


def test_a_nonsense_number_falls_back_instead_of_crashing(reload_config,
                                                          monkeypatch):
    monkeypatch.setenv("CPU_LOAD_PERCENT", "seventy")
    reloaded = reload_config()
    assert reloaded.CPU_LOAD_PERCENT == 70
    assert any("not a number" in warning for warning in reloaded.STARTUP_WARNINGS)


def test_out_of_range_values_are_clamped(reload_config, monkeypatch):
    monkeypatch.setenv("CPU_LOAD_PERCENT", "5000")
    assert reload_config().CPU_LOAD_PERCENT == 100


def test_code_archive_exclude_dirs_defaults_to_none(reload_config, monkeypatch):
    # None (not an empty list) means "not set" - archive.py knows to fall
    # back to its own default list rather than stripping nothing.
    monkeypatch.delenv("CODE_ARCHIVE_EXCLUDE_DIRS", raising=False)
    assert reload_config().CODE_ARCHIVE_EXCLUDE_DIRS is None


def test_code_archive_exclude_dirs_parses_a_comma_list(reload_config, monkeypatch):
    monkeypatch.setenv("CODE_ARCHIVE_EXCLUDE_DIRS", "vendor, dist ,node_modules")
    assert reload_config().CODE_ARCHIVE_EXCLUDE_DIRS == (
        "vendor", "dist", "node_modules")


def test_gitlab_credentials_are_only_required_for_pushing(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_USER", "octocat")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "x" * 12)
    monkeypatch.setattr(config, "GITLAB_USER", "your-gitlab-username")
    monkeypatch.setattr(config, "GITLAB_TOKEN", "")
    assert config.missing_credentials(gitlab=False) == []
    assert config.missing_credentials() == ["GITLAB_USER", "GITLAB_TOKEN"]


@pytest.mark.parametrize("value", ["", "your-gitlab-group"])
def test_an_unset_or_placeholder_group_means_no_group(reload_config,
                                                      monkeypatch, value):
    monkeypatch.setenv("GITLAB_GROUP", value)
    assert reload_config().GITLAB_GROUP is None


def test_below_the_minimum_is_clamped_up(reload_config, monkeypatch):
    monkeypatch.setenv("CPU_LOAD_PERCENT", "0")
    reloaded = reload_config()
    assert reloaded.CPU_LOAD_PERCENT == 1
    assert any("below the minimum" in w for w in reloaded.STARTUP_WARNINGS)


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("No", False), ("off", False),
    ("", True),                     # unset: the default
])
def test_include_forks_reads_true_and_false(reload_config, monkeypatch, value, expected):
    monkeypatch.setenv("INCLUDE_FORKS", value)
    assert reload_config().INCLUDE_FORKS is expected


def test_include_forks_nonsense_falls_back_with_a_warning(reload_config, monkeypatch):
    monkeypatch.setenv("INCLUDE_FORKS", "maybe")
    reloaded = reload_config()
    assert reloaded.INCLUDE_FORKS is True
    assert any("INCLUDE_FORKS='maybe' is not true/false" in w
               for w in reloaded.STARTUP_WARNINGS)
