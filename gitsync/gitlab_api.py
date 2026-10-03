"""The slice of the GitLab API GitSync needs.

Sessions are per-thread: requests.Session is not guaranteed thread-safe and the
sync runs several repos in parallel.
"""

import logging
import threading
from urllib.parse import quote

import requests

from . import config

logger = logging.getLogger(__name__)

_local = threading.local()

# Gitlab session


def session():
    existing = getattr(_local, "session", None)
    if existing is None:
        existing = requests.Session()
        existing.headers.update({"PRIVATE-TOKEN": config.GITLAB_TOKEN})
        _local.session = existing
    return existing

# GitLab answered, but not with anything we can use (auth, rate limit, outage)


class GitLabError(Exception):
    pass

# Check if the project exists on gitlab. None means GitLab says it does not
# exist; anything else going wrong raises, so a 401 or a 500 is never mistaken
# for "not there yet - create it".


def get_project(repo_name, user_name, group_id):
    # The API wants the namespace *path* ("my-group/repo"), not the numeric
    # group id - "12345/repo" is a 404 for every project, existing or not
    namespace = config.GITLAB_GROUP if group_id is not None else user_name
    proj_path = f"{namespace}/{repo_name}"
    encoded = quote(proj_path, safe='')
    url = f"{config.GITLAB_URL}/api/v4/projects/{encoded}"

    # Hit the api
    response = session().get(url, timeout=config.HTTP_TIMEOUT)
    if response.status_code == 200:
        logger.debug("Found GitLab project %s", proj_path)
        return response.json()
    if response.status_code == 404:
        logger.debug("GitLab project %s not found", proj_path)
        return None
    raise GitLabError(
        f"checking {proj_path} returned {response.status_code}: {response.text}")

# Get the gitlab group id


def get_group_id():
    if not config.GITLAB_GROUP:
        return None
    url = f"{config.GITLAB_URL}/api/v4/groups/{quote(config.GITLAB_GROUP, safe='')}"
    # Called from run_sync on the main thread, before any worker is started, so
    # SystemExit really does stop the run (in a worker it would just kill that
    # thread)
    try:
        response = session().get(url, timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as e:
        logger.error("Could not reach GitLab to look up group %s: %s",
                     config.GITLAB_GROUP, e)
        raise SystemExit("Could not reach GitLab")
    if response.status_code == 200:
        return response.json()["id"]
    logger.error("Could not look up GitLab group %s (status=%s). Response: %s",
                 config.GITLAB_GROUP, response.status_code, response.text)
    if response.status_code == 404:
        raise SystemExit("GitLab group not found - check GITLAB_GROUP")
    raise SystemExit(
        f"GitLab group lookup failed ({response.status_code}) - check GITLAB_TOKEN")

# Update visibility


def update_project_visibility(project_id, visibility):
    url = f"{config.GITLAB_URL}/api/v4/projects/{project_id}"
    data = {"visibility": visibility}
    response = session().put(url, data=data, timeout=config.HTTP_TIMEOUT)

    if response.status_code == 200:
        logger.info("Updated GitLab project %s visibility -> %s",
                    project_id, visibility)
        return response.json()
    else:
        logger.error("Failed to update visibility for project %s: %s %s",
                     project_id, response.status_code, response.text)
        return None

# Create Gitlab Project


def create_project(group_id, repo_name, visibility="private"):
    url = f"{config.GITLAB_URL}/api/v4/projects"
    data = {
        "name": repo_name,
        "path": repo_name,
        "visibility": visibility,
        "initialize_with_readme": False
    }

    if group_id is not None:
        data["namespace_id"] = group_id

    response = session().post(url, data=data, timeout=config.HTTP_TIMEOUT)
    if response.status_code not in (201, 200):
        logger.error("Failed to create project %s on GitLab: %s %s",
                     repo_name, response.status_code, response.text)
        return None

    logger.info("Created GitLab project %s", repo_name)
    return response.json()

# Let force pushes through every protected branch of a project, for a mirror
# whose history was rewritten on GitHub (see config.GITLAB_ALLOW_FORCE_PUSH).
# Only branches that don't allow it yet are changed. Returns how many were;
# raises GitLabError if the project or the branches can't be read or changed.


def allow_force_push(repo_name, user_name, group_id) -> int:
    project = get_project(repo_name=repo_name, user_name=user_name, group_id=group_id)
    if not project:
        raise GitLabError(f"project {repo_name} not found on GitLab")
    base = f"{config.GITLAB_URL}/api/v4/projects/{project['id']}/protected_branches"

    response = session().get(base, params={"per_page": 100},
                             timeout=config.HTTP_TIMEOUT)
    if response.status_code != 200:
        raise GitLabError(f"listing protected branches of {repo_name} returned "
                          f"{response.status_code}: {response.text}")

    changed = 0
    for branch in response.json():
        if branch.get("allow_force_push"):
            continue
        # The name goes in the path: "release/*" must become "release%2F%2A"
        url = f"{base}/{quote(branch['name'], safe='')}"
        update = session().patch(url, data={"allow_force_push": "true"},
                                 timeout=config.HTTP_TIMEOUT)
        if update.status_code != 200:
            raise GitLabError(f"allowing force push on {repo_name}:{branch['name']} "
                              f"returned {update.status_code}: {update.text}")
        logger.info("Allowed force push on protected branch %s of %s",
                    branch["name"], repo_name)
        changed += 1
    return changed
