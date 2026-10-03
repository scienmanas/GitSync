# Contributing to GitSync

Thanks for wanting to help! Bug reports, ideas, docs fixes and code are all welcome. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md), and report security problems privately as described in [SECURITY.md](SECURITY.md) — not as an issue.

## Ways to help

- **Report a bug** — open an issue with the *Bug report* form. The log lines matter most; tokens are masked in GitSync's logs, but double-check before pasting.
- **Suggest a feature** — use the *Feature request* form, after checking [Coming in 3.0.0](README.md#coming-in-300-) in case it's already planned.
- **Test on Windows** — GitSync is only tested on macOS and Linux. If you try it on Windows, an issue describing what happened is very welcome.
- **Pick up an issue** — if an issue isn't assigned (except to the maintainer), say in the issue or in Discussions that you'd like to work on it, and it'll be assigned to you.

## Setting up

You need [uv](https://docs.astral.sh/uv/) 0.12.22 or newer (`uv self update` if yours is older) and git. uv fetches Python 3.14 for you.

```sh
git clone https://github.com/scienmanas/GitSync.git
cd GitSync
uv sync              # creates .venv with Python 3.14, the dependencies and pytest
uv run pytest        # everything should pass before you start
```

No tokens, network or Docker daemon are needed for the tests. To try GitSync for real, copy `.env.example` to `.env` and fill in a GitHub token (and GitLab, for `sync`).

## Making a change

1. **Branch from `main`** with a descriptive name, e.g. `fix/gitlab-group-lookup` or `feat/github-orgs`.
2. **Add a test for new behaviour.** The existing tests show the patterns:
   - real throwaway git repos via the `repo_factory` and `mirrors_dir` fixtures (`tests/conftest.py`),
   - stubbed GitHub/GitLab sessions (`FakeSession` in `tests/test_api.py`),
   - a fake clock for anything scheduled (`FakeClock` in `tests/test_schedule.py`).
3. **Run the suite**: `uv run pytest` (or `uv run pytest -q -o addopts=""` for short output). `ERROR` lines in the output are logs from tests that break things on purpose; only `FAILED` is a failure.
4. **Update the docs** if a setting, command or behaviour changed — `README.md`, and `.env.example` for new settings.
5. **Add a line to `CHANGELOG.md`** under `[Unreleased]`.
6. **Open a pull request.** The template has a short checklist. CI runs the tests and a dependency audit (`pip-audit`) on every pull request.

## Code style

Match the code around you:

- Comments explain *why*, above the code they're about — the reasoning a reader can't get from the code itself.
- Log what happened in words a user can act on; never log or print a token (route new output through the logger so it gets masked).
- No new runtime dependencies without a good reason — GitSync deliberately needs only `requests`, `rich` and `python-dotenv`.

## Releases

The version lives in `pyproject.toml` and `gitsync/__init__.py`. A release moves the `[Unreleased]` notes in `CHANGELOG.md` under the new version, tags it (`v2.0.0`), and publishes the Docker image with **Actions → Publish image**.
