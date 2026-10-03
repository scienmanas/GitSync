# Security Policy

GitSync handles GitHub and GitLab access tokens, so security reports are taken seriously.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 2.x     | ✅        |
| 1.x     | ❌ — please upgrade to 2.x |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately instead, either way:

- **GitHub** (preferred): [open a private security advisory](https://github.com/scienmanas/GitSync/security/advisories/new) — only the maintainer can see it.
- **Email**: iamscientistmanas@gmail.com, with `GitSync security` in the subject.

Include what you found, how to reproduce it, and which version you ran (`pyproject.toml`, or the image tag if you use Docker). **Never include a real token** — if one leaked, revoke it first and describe where it appeared.

You can expect an acknowledgement within a few days. Once a fix is released, the report is credited in [CHANGELOG.md](CHANGELOG.md) unless you'd rather stay anonymous.

## What counts

Especially welcome:

- A token reaching a log file, the terminal, `docker logs`, a crash traceback or an archive — GitSync is meant to mask every one of them.
- Anything that lets a backup be written outside the configured folders, or a run touch mirrors it does not own.
- Problems in the Docker image or entrypoint (running as root, writable paths, secrets baked into a layer).

## Keeping your own setup safe

- Give tokens the least access they need: read access for GitHub; `api` and `write_repository` for GitLab, and only if you use `sync`.
- Keep `.env` out of version control (it is in `.gitignore`) and out of images (it is in `.dockerignore`).
- Log files written by GitSync 1.x may contain tokens. Check them before sharing, and revoke any token that appears.
