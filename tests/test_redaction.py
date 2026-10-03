"""Issue #20: nothing credential-shaped may survive redact()."""

import pytest

from gitsync import redaction
from gitsync.redaction import REDACTED, redact

from .conftest import FAKE_GITHUB_TOKEN, FAKE_GITLAB_TOKEN

pytestmark = pytest.mark.usefixtures("known_secrets")


@pytest.mark.parametrize("text, expected", [
    # The two URL shapes GitSync builds itself
    (f"https://octocat:{FAKE_GITHUB_TOKEN}@github.com/octocat/repo.git",
     "https://octocat:***@github.com/octocat/repo.git"),
    (f"https://oauth2:{FAKE_GITLAB_TOKEN}@gitlab.com/octolab/repo.git",
     "https://oauth2:***@gitlab.com/octolab/repo.git"),
    # Token as the whole userinfo
    (f"https://{FAKE_GITHUB_TOKEN}@github.com/x.git",
     "https://***@github.com/x.git"),
    # What git echoes back on an auth failure
    (f"remote: Invalid username or password for https://octocat:{FAKE_GITHUB_TOKEN}@github.com",
     "remote: Invalid username or password for https://octocat:***@github.com"),
])
def test_masks_credentials_in_urls(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("text, expected", [
    # Fake credentials, split so secret scanners don't take them for real ones
    ("https://someone:" + "hunter2-not-registered@example.com/x.git",
     "https://someone:***@example.com/x.git"),
    ("https://unregistered-secret-value@example.com/x.git",
     "https://***@example.com/x.git"),
    ("fatal: Authentication failed for 'https://bob:" + "p4ssw0rd@git.example.com/r.git'",
     "fatal: Authentication failed for 'https://bob:***@git.example.com/r.git'"),
])
def test_masks_credentials_it_was_never_handed(text, expected):
    # Masking is structural, not just a search for the tokens we know: a
    # rotated token left in a stale remote, or another tool's URL echoed by
    # git, has to be caught on shape alone.
    assert redact(text) == expected


def test_masks_bare_configured_tokens():
    assert redact(f"token={FAKE_GITLAB_TOKEN} other={FAKE_GITHUB_TOKEN}") == \
        f"token={REDACTED} other={REDACTED}"


def test_masks_configured_tokens_whatever_they_look_like():
    # A self-hosted or enterprise token need not look like ghp_*/glpat-*, and
    # outside a URL there is no structure to go on - only the value itself.
    odd = "9f3a-plain-looking-value"
    redaction.register_secret(odd)
    try:
        assert redact(f"PRIVATE-TOKEN: {odd}") == f"PRIVATE-TOKEN: {REDACTED}"
    finally:
        redaction._SECRET_VALUES.discard(odd)


@pytest.mark.parametrize("token", [
    # Built from two pieces so the token shapes never appear literally in the
    # source - secret scanners would take them for real tokens
    "ghp_" + "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
    "github_pat_" + "x" * 30,
    "glpat-" + "nEvErSeEnThIsOnE1234",
])
def test_masks_token_shapes_we_were_never_handed(token):
    # An API error body or a git hint can carry someone else's token
    assert token not in redact(f"unexpected: {token}")


@pytest.mark.parametrize("text", [
    "plain https://github.com/octocat/repo.git stays intact",
    "mail me at user@example.com please",
    "Found 42 GitHub repos",
    "fatal: repository 'https://gitlab.com/o/r.git/' not found",
])
def test_leaves_ordinary_text_alone(text):
    assert redact(text) == text


def test_handles_non_strings():
    assert redact(None) is None
    assert redact(42) == "42"
    assert redact(RuntimeError(f"boom {FAKE_GITHUB_TOKEN}")) == "boom ***"


def test_unregistered_short_values_are_not_masked():
    # Placeholder/no-op values must stay readable so a misconfiguration is
    # obvious in the logs instead of showing up as ***
    assert redact("your-github-token") == "your-github-token"
