"""Keeps credentials out of everything GitSync writes.

Tokens are embedded in the clone/push URLs and git happily echoes those back on
failures, so nothing reaches the log file, the terminal or the rich UI buffer
before passing through redact().
"""

import re

from . import config

REDACTED = "***"

# https://user:token@host -> https://user:***@host  (and https://token@host)
# Named groups: scheme (e.g. "https://"), user (empty when the token itself is
# the userinfo), secret (None when there's no ":password" part at all - that's
# what tells _mask_url_credentials which of the two shapes it matched).
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/@\s]*)(?::(?P<secret>[^@/\s]*))?@")

# Well-known token shapes, in case one shows up that we were never handed.
_TOKEN_PATTERNS = (
    # gh[pousr]_ expands to 5 real GitHub prefixes: ghp_ (personal access
    # token), gho_ (oAuth), ghu_ (App user-to-server), ghs_ (App
    # server-to-server), ghr_ (App refresh) - one letter per token kind.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),              # GitHub classic/fine
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),            # GitHub fine-grained
    # GitLab prefixes its own tokens the same way (glpat- personal access,
    # gldt- deploy, glrt- runner, ...) - matched by shape instead of listing
    # every kind by name, so a new GitLab prefix is still caught.
    re.compile(r"gl[a-z]{2,6}-[A-Za-z0-9_\-]{16,}"),        # glpat-, gldt-, ...
)

# Exact secrets we know about
_SECRET_VALUES = set()

# Teach the redactor about a value that must never be printed. Skips the
# .env.example placeholder strings (nothing secret about those) and anything
# shorter than 6 chars, since a short value is unlikely to be a real token and
# blind string-replacing it risks mangling unrelated log text it happens to
# appear inside of.


def register_secret(value) -> None:
    if value and value not in config.PLACEHOLDER_VALUES and len(value) >= 6:
        _SECRET_VALUES.add(value)


for _token in (config.GITHUB_TOKEN, config.GITLAB_TOKEN):
    register_secret(_token)

# Replace the credential part of a matched URL, keeping the rest readable


def _mask_url_credentials(match) -> str:
    scheme = match.group("scheme")
    if match.group("secret") is None:
        # Single-part userinfo, i.e. the token itself is the "username"
        return f"{scheme}{REDACTED}@"
    return f"{scheme}{match.group('user')}:{REDACTED}@"

# Strip anything credential-shaped out of a value before it is shown/stored


def redact(value):
    if value is None:
        return value
    # Callers pass log records, exceptions, ints - anything, not just strings.
    # isinstance() skips the str() call in the common case (already a string);
    # str(value) does the real work of turning an exception/int/etc. into text.
    text = value if isinstance(value, str) else str(value)
    # Pass 1: exact match - tokens we were explicitly told about, via register_secret().
    for secret in tuple(_SECRET_VALUES):
        text = text.replace(secret, REDACTED)
    # Pass 2: structural match - catches a token we were never handed (rotated,
    # or someone else's), as long as it sits in a scheme://user[:secret]@ shape.
    text = _URL_CREDENTIALS_RE.sub(_mask_url_credentials, text)
    # Pass 3: shape match - catches a known token prefix outside a URL entirely,
    # e.g. sitting in an API error body.
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
