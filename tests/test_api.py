"""The GitHub/GitLab API wrappers: timeouts, and telling errors from "not found"."""

import pytest
import requests

from gitsync import config, github_api, gitlab_api, sync


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Hands back queued responses (or raises queued exceptions) in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._next("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._next("PATCH", url, **kwargs)


@pytest.fixture
def gitlab(monkeypatch):
    def install(*responses):
        fake = FakeSession(*responses)
        monkeypatch.setattr(gitlab_api, "session", lambda: fake)
        return fake
    return install


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setattr(config, "SLEEP_BETWEEN_API", 0)

    def install(*responses):
        fake = FakeSession(*responses)
        monkeypatch.setattr(github_api, "session", lambda: fake)
        return fake
    return install


# --------------------------------------------------------------------------- #
# GitLab                                                                       #
# --------------------------------------------------------------------------- #

def test_group_projects_are_looked_up_by_group_path(monkeypatch, gitlab):
    monkeypatch.setattr(config, "GITLAB_GROUP", "my-group")
    fake = gitlab(FakeResponse(200, {"id": 1}))
    gitlab_api.get_project(repo_name="repo-a", user_name="me", group_id=12345)
    # The path, never the numeric id - "12345/repo-a" 404s for every project
    assert fake.calls[0][1].endswith("/projects/my-group%2Frepo-a")


def test_user_projects_are_looked_up_by_user_name(gitlab):
    fake = gitlab(FakeResponse(200, {"id": 1}))
    gitlab_api.get_project(repo_name="repo-a", user_name="me", group_id=None)
    assert fake.calls[0][1].endswith("/projects/me%2Frepo-a")


def test_only_a_404_means_not_found(gitlab):
    gitlab(FakeResponse(404))
    assert gitlab_api.get_project("repo-a", "me", None) is None


@pytest.mark.parametrize("status", [401, 429, 500])
def test_other_errors_are_not_mistaken_for_not_found(gitlab, status):
    gitlab(FakeResponse(status))
    with pytest.raises(gitlab_api.GitLabError):
        gitlab_api.get_project("repo-a", "me", None)


def test_ensure_does_not_create_when_the_check_fails(gitlab, captured_log):
    fake = gitlab(FakeResponse(500))
    assert sync.ensure_gitlab_project(None, "repo-a", "me", "private") is False
    assert [method for method, _, _ in fake.calls] == ["GET"]  # no POST
    assert any("Could not check GitLab project repo-a" in line for line in captured_log)


def test_ensure_survives_a_network_error(gitlab):
    gitlab(requests.ConnectionError("down"))
    assert sync.ensure_gitlab_project(None, "repo-a", "me", "private") is False


def test_ensure_creates_a_missing_project(gitlab):
    fake = gitlab(FakeResponse(404), FakeResponse(201, {"id": 7}))
    assert sync.ensure_gitlab_project(None, "repo-a", "me", "private") is True
    assert [method for method, _, _ in fake.calls] == ["GET", "POST"]


def test_every_gitlab_call_has_a_timeout(gitlab):
    fake = gitlab(FakeResponse(200, {"id": 7, "visibility": "public"}),
                  FakeResponse(200, {"id": 7}),
                  FakeResponse(201, {"id": 8}))
    gitlab_api.get_project("repo-a", "me", None)
    gitlab_api.update_project_visibility(7, "private")
    gitlab_api.create_project(None, "repo-b")
    assert all(kwargs.get("timeout") == config.HTTP_TIMEOUT
               for _, _, kwargs in fake.calls)


def test_group_lookup_timeout_stops_the_run_cleanly(monkeypatch, gitlab):
    monkeypatch.setattr(config, "GITLAB_GROUP", "my-group")
    gitlab(requests.Timeout("slow"))
    with pytest.raises(SystemExit):
        gitlab_api.get_group_id()


# --------------------------------------------------------------------------- #
# GitHub                                                                       #
# --------------------------------------------------------------------------- #

def test_repos_are_paged_until_an_empty_page(github):
    fake = github(FakeResponse(200, [{"name": "a"}]),
                  FakeResponse(200, [{"name": "b"}]),
                  FakeResponse(200, []))
    assert [r["name"] for r in github_api.get_repos()] == ["a", "b"]
    assert all(kwargs.get("timeout") == config.HTTP_TIMEOUT
               for _, _, kwargs in fake.calls)


@pytest.mark.parametrize("failure", [FakeResponse(401),
                                     requests.ConnectionError("down")])
def test_a_failed_listing_is_none_not_empty(github, failure):
    # Even after a good first page: a partial list is not the account
    github(FakeResponse(200, [{"name": "a"}]), failure)
    assert github_api.get_repos() is None


def test_sync_fails_when_github_cannot_be_listed(monkeypatch, tmp_path):
    monkeypatch.setattr(github_api, "get_repos", lambda: None)
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setattr(config, "missing_credentials", lambda **kw: [])
    assert sync.run_sync() == 1


# --------------------------------------------------------------------------- #
# GitLab project creation, visibility and group lookup                         #
# --------------------------------------------------------------------------- #

def test_a_group_project_is_created_in_the_group(gitlab):
    fake = gitlab(FakeResponse(201, {"id": 9}))
    assert gitlab_api.create_project(42, "repo-a", "private") == {"id": 9}
    data = fake.calls[0][2]["data"]
    assert data["namespace_id"] == 42
    assert data["visibility"] == "private"
    assert data["initialize_with_readme"] is False


def test_a_user_project_has_no_namespace(gitlab):
    fake = gitlab(FakeResponse(201, {"id": 9}))
    gitlab_api.create_project(None, "repo-a")
    assert "namespace_id" not in fake.calls[0][2]["data"]


def test_a_refused_create_or_update_returns_none(gitlab):
    gitlab(FakeResponse(400, {"message": "has already been taken"}),
           FakeResponse(403, {"message": "forbidden"}))
    assert gitlab_api.create_project(None, "repo-a") is None
    assert gitlab_api.update_project_visibility(7, "public") is None


def test_no_group_means_no_lookup(monkeypatch, gitlab):
    monkeypatch.setattr(config, "GITLAB_GROUP", None)
    fake = gitlab()
    assert gitlab_api.get_group_id() is None
    assert fake.calls == []


def test_the_group_id_is_looked_up_by_path(monkeypatch, gitlab):
    monkeypatch.setattr(config, "GITLAB_GROUP", "parent/child")
    fake = gitlab(FakeResponse(200, {"id": 42}))
    assert gitlab_api.get_group_id() == 42
    assert fake.calls[0][1].endswith("/groups/parent%2Fchild")


@pytest.mark.parametrize("status, message", [(404, "GITLAB_GROUP"),
                                             (401, "GITLAB_TOKEN")])
def test_a_failed_group_lookup_names_the_likely_cause(monkeypatch, gitlab,
                                                     status, message):
    monkeypatch.setattr(config, "GITLAB_GROUP", "my-group")
    gitlab(FakeResponse(status))
    with pytest.raises(SystemExit, match=message):
        gitlab_api.get_group_id()


def test_sessions_carry_credentials_and_are_per_thread(monkeypatch):
    import threading
    monkeypatch.setattr(config, "GITLAB_TOKEN", "glpat-x")
    monkeypatch.setattr(config, "GITHUB_USER", "octocat")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp-x")
    for module in (gitlab_api, github_api):
        monkeypatch.setattr(module, "_local", threading.local())
    assert gitlab_api.session().headers["PRIVATE-TOKEN"] == "glpat-x"
    assert github_api.session().auth == ("octocat", "ghp-x")
    assert gitlab_api.session() is gitlab_api.session()      # reused in a thread
    other = []
    t = threading.Thread(target=lambda: other.append(gitlab_api.session()))
    t.start(); t.join()
    assert other[0] is not gitlab_api.session()               # but not shared


def test_only_repos_you_own_are_requested(github):
    # This release backs up your own repos: organization and collaborator
    # repos are not asked for (and "member" is not a value GitHub accepts)
    fake = github(FakeResponse(200, []))
    github_api.get_repos()
    url = fake.calls[0][1]
    assert "affiliation=owner" in url
    assert "member" not in url and "collaborator" not in url



# --------------------------------------------------------------------------- #
# Allowing force push on protected branches                                    #
# --------------------------------------------------------------------------- #

def test_force_push_is_allowed_on_every_protected_branch_that_blocks_it(gitlab):
    fake = gitlab(FakeResponse(200, {"id": 7}),                 # the project
                  FakeResponse(200, [{"name": "main", "allow_force_push": False},
                                     {"name": "release/*", "allow_force_push": False},
                                     {"name": "dev", "allow_force_push": True}]),
                  FakeResponse(200, {}), FakeResponse(200, {}))
    assert gitlab_api.allow_force_push("repo-a", "me", None) == 2
    patches = [(url, kw["data"]) for method, url, kw in fake.calls if method == "PATCH"]
    assert [url.rsplit("/", 1)[-1] for url, _ in patches] == ["main", "release%2F%2A"]
    assert all(data == {"allow_force_push": "true"} for _, data in patches)
    assert all(kw.get("timeout") == config.HTTP_TIMEOUT for _, _, kw in fake.calls)


def test_a_refused_change_is_an_error_not_a_silent_skip(gitlab):
    gitlab(FakeResponse(200, {"id": 7}),
           FakeResponse(200, [{"name": "main", "allow_force_push": False}]),
           FakeResponse(403, {"message": "403 Forbidden"}))
    with pytest.raises(gitlab_api.GitLabError, match="403"):
        gitlab_api.allow_force_push("repo-a", "me", None)


def test_allowing_force_push_needs_the_project_to_exist(gitlab):
    gitlab(FakeResponse(404))
    with pytest.raises(gitlab_api.GitLabError, match="not found"):
        gitlab_api.allow_force_push("repo-a", "me", None)
