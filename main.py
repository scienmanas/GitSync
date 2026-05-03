import os
import sys
import subprocess
from dotenv.main import logger
import requests
from dotenv import load_dotenv
import shutil
import logging
import time
from urllib.parse import quote
from collections import deque

# Rich UI
from rich.live import Live
from rich.panel import Panel
from rich.console import Group
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, MofNCompleteColumn


# Laod Env variables
load_dotenv()

#  Configs
GITHUB_USER = os.getenv("GITHUB_USER", "your-github-username")
GITHUB_TOKEN = os.getenv(
    "GITHUB_TOKEN", "your-github-token")   # repo read access
GITLAB_USER = os.getenv("GITLAB_USER", "your-gitlab-username")
GITLAB_GROUP = os.getenv("GITLAB_GROUP", None)
# api + write_repository (or api)
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "your-gitlab-token")
REPO_VISIBILITY = os.getenv("REPO_VISIBILITY", "auto")
PER_PAGE = 100
BACKUP_DIR = "./repos-backup"
GITLAB_URL = "https://gitlab.com"
LOGS_FOLDER = "Logs"
SLEEP_BETWEEN_API = 0.5  # seconds

# Ensure the backup and logs directories exist
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

# logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
# Generate log filename with timestamp
log_timestamp = time.strftime("%Y%m%d_%H%M%S")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_FOLDER = os.path.join(PROJECT_DIR, "Logs")
LOG_FILE = f"logs_{log_timestamp}.txt"
LOG_FILE_LOCATION = os.path.join(LOGS_FOLDER, LOG_FILE)
file_handler = logging.FileHandler(LOG_FILE_LOCATION, mode="a")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
# logs buffer for the UI (most recent N lines)
LOG_MAX_LINES = 300
logs_deque = deque(maxlen=LOG_MAX_LINES)
console = Console()

# Rich-aware logger handler that appends to logs_deque


class RichBufferHandler(logging.Handler):
    def __init__(self, buffer_deque) -> None:
        super().__init__()
        self.buffer = buffer_deque

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        # Append to deque (keeps most recent lines)
        for line in msg.splitlines():
            self.buffer.append(line)


# Add a rich UI handler for logs
rich_handler = RichBufferHandler(logs_deque)
rich_handler.setFormatter(formatter)
logger.addHandler(rich_handler)

# Github session
gh_session = requests.Session()
gh_session.auth = (GITHUB_USER, GITHUB_TOKEN)
gh_session.headers.update({"Accept": "application/vnd.github.v3+json"})

# Gitlab session
gl_session = requests.Session()
gl_headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
gl_session.headers.update(gl_headers)

# Runs cmd commands


def run(cmd, cwd=None, check=True) -> None:
    logger.info("RUN: %s (cwd=%s)", " ".join(cmd), cwd or ".")
    # Use Popen so we can stream output line-by-line
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True
    )
    # Stream lines as they appear
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            # log each line - will go to file and to the rich UI buffer
            logger.info(line)
        proc.wait()
    except Exception as e:
        proc.kill()
        raise
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)

# Get all the repos from github


def get_github_repos():
    repos = []
    page = 1
    while True:
        # Hit the URL
        url = f"https://api.github.com/user/repos?per_page={PER_PAGE}&page={page}&&affiliation=owner,member"
        response = gh_session.get(url=url)
        if response.status_code != 200:
            logger.error("GitHub API error %s: %s", response.status_code, response.text)
            break

        # Get data in json format
        batch = response.json()
        if not batch:
            break

        repos.extend(batch)
        page += 1
        time.sleep(SLEEP_BETWEEN_API)

    logger.info("Found %d GitHub repos", len(repos))
    return repos

# Check if the project exists on gitlab


def get_gitlab_project(repo_name, user_name, group_id):
    if group_id is not None:
        proj_path = f"{group_id}/{repo_name}"
    else:
        proj_path = f"{user_name}/{repo_name}"
    #  Get the project path
    encoded = quote(proj_path, safe='')
    url = f"{GITLAB_URL}/api/v4/projects/{encoded}"

    # Hit the api
    response = gl_session.get(url)
    if response.status_code == 200:
        logger.debug("Found GitLab project %s", proj_path)
        return response.json()
    elif response.status_code == 404:
        logger.debug("GitLab project %s not found", proj_path)
        return None
    else:
        logger.error("Error checking project %s: %s %s",
                     proj_path, response.status_code, response.text)
        return None

# Get the gitlab user id


def get_gitlab_group_id():
    if not GITLAB_GROUP:
        return None
    url = f"{GITLAB_URL}/api/v4/groups/{quote(GITLAB_GROUP, safe='')}"
    response = gl_session.get(url)
    if response.status_code == 200:
        return response.json()["id"]
    logger.error("Could not find GitLab group %s (status=%s). Response: %s",
                 GITLAB_GROUP, response.status_code, response.text)
    raise SystemExit("GitLab group not found - check GITLAB_GROUP")

# Update visibility


def update_gitlab_project_visibility(project_id, visibility):
    url = f"{GITLAB_URL}/api/v4/projects/{project_id}"
    data = {"visibility": visibility}
    response = gl_session.put(url, data=data)

    if response.status_code == 200:
        logger.info("Updated GitLab project %s visibility -> %s",
                    project_id, visibility)
        return response.json()
    else:
        logger.error("Failed to update visibility for project %s: %s %s",
                     project_id, response.status_code, response.text)
        return None
# Create Gitlab Project


def create_gitlab_project(group_id, repo_name, visibility="private"):
    url = f"{GITLAB_URL}/api/v4/projects"
    data = {
        "name": repo_name,
        "path": repo_name,
        "visibility": visibility,
        "initialize_with_readme": False
    }

    if group_id is not None:
        data["namespace_id"] = group_id

    response = gl_session.post(url, data=data)
    if response.status_code not in (201, 200):
        logger.error("Failed to create project %s on GitLab: %s %s",
                     repo_name, response.status_code, response.text)

    logger.info("Created GitLab project %s", repo_name)
    return response.json()

# Mirror the github repos to local


def mirror_repos_from_github(repo_name, github_url, local_path) -> None:
    # Build URL to clone from
    if github_url.startswith("https://"):
        auth_clone_url = github_url.replace(
            "https://", f"https://{GITHUB_USER}:{GITHUB_TOKEN}@", 1)
    else:
        auth_clone_url = github_url

    # Create a local copy of the github repo
    if not os.path.exists(local_path):
        logger.info("Clonning (mirror) %s ...", repo_name)
        try:
            run(["git", "clone", "--mirror", auth_clone_url, local_path])
        except Exception as e:
            logger.exception("Failed to clone %s: %s", repo_name, e)
    else:
        try:
            run(["git", "--git-dir", local_path, "fetch", "--all", "--prune"])
        except Exception as e:
            logger.exception("Failed to clone %s: %s", repo_name, e)
            # Attemp a reclone if corrupted
            try:
                logger.info("Recloning %s due to fetch failure", repo_name)
                shutil.rmtree(local_path) # delete the corrupted repo
                run(["git", "clone", "--mirror", auth_clone_url, local_path])
            except Exception as e2:
                logger.exception("Reclone failed for %s: %s", repo_name, e2)

# Chech repo in gitlab exists or not, if not make it


def check_and_validate_gitlab_repos(group_id, repo_name, user_name, repo_visibility) -> None:
    proj = get_gitlab_project(
        user_name=user_name, group_id=group_id, repo_name=repo_name)
    if not proj:
        logger.info("Project %s not found on GitLab. Creating...", repo_name)
        try:
            create_gitlab_project(
                group_id=group_id, repo_name=repo_name, visibility=repo_visibility)
        except Exception as e:
            logger.exception(
                "Could not create GitLab project %s: %s", repo_name, e)
    else:
        current_visibility = proj.get("visibility")
        if current_visibility != repo_visibility:
            logger.info("Project %s exists on GitLab with visibility '%s' but desired is '%s'. Updating...",
                        repo_name, current_visibility, repo_visibility)
            try:
                update_gitlab_project_visibility(proj["id"], repo_visibility)
            except Exception as e:
                logger.exception(
                    "Failed to update visibility for %s: %s", repo_name, e)
        else:
            logger.info("Project %s exists on GitLab with matching visibility '%s'.",
                        repo_name, current_visibility)

# Sync the repos to gitlab


def sync_repos(group_id, user_name, gitlab_token, repo_name, local_path) -> None:
    # Push the mirror to the gitlab
    target_namespace = GITLAB_GROUP if group_id else user_name
    gl_repo_url = f"{GITLAB_URL}/{target_namespace}/{repo_name}.git"
    if gl_repo_url.startswith("https://"):
        push_url = gl_repo_url.replace(
            "https://", f"https://oauth2:{gitlab_token}@", 1)
    else:
        push_url = gl_repo_url

    logger.info("Pushing %s -> GitLab (%s) ...", repo_name, target_namespace)
    try:
        run(["git", "--git-dir", local_path, "push", "--mirror", push_url])
    except Exception as e:
        logger.exception("Push failed for %s: %s", repo_name, e)

# Main function


def main() -> None:
    try:
        # Get the repos links and other stuff
        repos_done = 0
        repos = get_github_repos()
        gitlab_group_id = get_gitlab_group_id()
        if not repos:
            logger.info("No repos found; exiting.")
            return

        # Build a progress bar (top)
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),      # shows "n / total"
            TimeElapsedColumn(),
            expand=True,
            console=console
        )
        task = progress.add_task(
            description="🔄 Syncing Repos", total=len(repos))

        # Live UI group: progress on top, logs panel below
        def make_layout(current_repo: str):
            # join last N lines for the logs panel
            # Show only the last 20 lines
            logs_text = "\n".join(list(logs_deque)[-50:] or ["(no logs yet)"])
            panel = Panel(
                logs_text, title=f"Logs - {escape(current_repo)}", border_style="green", expand=True)
            return Group(progress, panel)

        with Live(make_layout("starting..."), refresh_per_second=1, transient=True, console=console) as live:
            for repo in repos:
                repo_name = repo["name"]
                github_url = repo["clone_url"]
                repo_visibility = ""
                if REPO_VISIBILITY is None or len(REPO_VISIBILITY) == 0:
                    repo_visibility = "private"
                elif REPO_VISIBILITY == "auto":
                    if "private" in repo:
                        repo_visibility = "private" if repo["private"] else "public"
                else:
                    repo_visibility = REPO_VISIBILITY

                local_path = os.path.join(BACKUP_DIR, f"{repo_name}.git")

                # update current repo in progress bar
                progress.update(task, description=f"🔄 Syncing {repo_name}")

                # clear logs so each repo has fresh log panel
                logs_deque.clear()

                try:
                    mirror_repos_from_github(
                        repo_name=repo_name, github_url=github_url, local_path=local_path)
                    check_and_validate_gitlab_repos(
                        group_id=gitlab_group_id, repo_name=repo_name, user_name=GITLAB_USER, repo_visibility=repo_visibility)
                    sync_repos(group_id=gitlab_group_id, user_name=GITLAB_USER,
                               gitlab_token=GITLAB_TOKEN, repo_name=repo_name, local_path=local_path)
                    logger.info("✅ Synced %s", repo_name)
                except Exception as e:
                    logger.error("❌ Failed %s: %s", repo_name, e)

                # refresh the live layout to show latest logs
                repos_done += 1
                logger.info("Repos done: %s/%s", repos_done, len(repos))
                progress.advance(task, 1)
                live.update(make_layout(current_repo=repo_name))

        # Final success message
        logger.info(
            "✅ All Done :), all repositories has been synced, please check the logs for details.")
        print("✅ All Done :), Now you can enjoy hehe, please check the logs for details.")
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
    except Exception as e:
        logger.exception("Something went wrong: %s", e)


# Run the programme
if __name__ == "__main__":
    # Ensure the logs directory exists
    logger.info(
        "####################### logger Started ############################")
    logger.info("#################### Timestamp: %s ######################",
                time.strftime("%Y-%m-%d %H:%M:%S"))

    # ENV checks
    if not GITHUB_TOKEN:
        logger.error("GITHUB_TOKEN environment variable is not set.")
        print("GITHUB_TOKEN environment variable is not set.")
        sys.exit(1)
    if not GITLAB_TOKEN:
        logger.error("GITLAB_TOKEN environment variable is not set.")
        print("GITLAB_TOKEN environment variable is not set.")
        sys.exit(1)
    if not GITLAB_USER:
        logger.error("GITLAB_USER environment variable is not set.")
        print("GITLAB_USER environment variable is not set.")
        sys.exit(1)

    # After all check start the program
    main()
