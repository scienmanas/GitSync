"""The slice of the GitHub API GitSync needs.

Sessions are per-thread: requests.Session is not guaranteed thread-safe and the
sync runs several repos in parallel.
"""

import logging
import threading
import time

import requests

from . import config

logger = logging.getLogger(__name__)

_local = threading.local()

# Github session


def session():
    existing = getattr(_local, "session", None)
    if existing is None:
        existing = requests.Session()
        existing.auth = (config.GITHUB_USER, config.GITHUB_TOKEN)
        existing.headers.update({"Accept": "application/vnd.github.v3+json"})
        _local.session = existing
    return existing

# Get all the repos from github. None means the list could not be fetched -
# kept apart from [] (an account with no repos) so a bad token or a GitHub
# outage fails the run instead of passing as "nothing to sync".


def get_repos():
    repos = []
    page = 1
    gh_session = session()
    while True:
        # Hit the URL. affiliation=owner: repos owned by your own account -
        # what this release backs up. Organization repos (even of an org you
        # own - they belong to the org, not to you) and repos you only
        # collaborate on are left out for now.
        url = (f"https://api.github.com/user/repos?per_page={config.PER_PAGE}"
               f"&page={page}&affiliation=owner")
        try:
            response = gh_session.get(url=url, timeout=config.HTTP_TIMEOUT)
        except requests.RequestException as e:
            logger.error("Could not reach GitHub (page %d): %s", page, e)
            return None
        if response.status_code != 200:
            logger.error("GitHub API error %s (page %d): %s",
                         response.status_code, page, response.text)
            return None

        # Get data in json format
        batch = response.json()
        if not batch:
            break

        repos.extend(batch)
        page += 1
        time.sleep(config.SLEEP_BETWEEN_API)

    logger.info("Found %d GitHub repos", len(repos))
    return repos
