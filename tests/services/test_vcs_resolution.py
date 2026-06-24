from types import SimpleNamespace
from reviewer.config.settings import Settings
from reviewer.services.review_service import ReviewService
from reviewer.vcs.github import GitHubProvider
from reviewer.vcs.gitlab import GitLabProvider


def _service(repo_vcs_row):
    settings = Settings(github_token="gh", gitlab_token="gl", vcs_provider="github")
    store = SimpleNamespace(get_repo_vcs=lambda repo: repo_vcs_row)
    components = SimpleNamespace(store=store)
    return ReviewService(settings, components)


def test_resolves_gitlab_when_repo_vcs_says_gitlab():
    svc = _service(("gitlab", "https://gitlab.acme.com"))
    p = svc._create_vcs_provider("o", "r")
    assert isinstance(p, GitLabProvider)
    p.close()


def test_falls_back_to_env_default_github_when_absent():
    svc = _service(None)
    p = svc._create_vcs_provider("o", "r")
    assert isinstance(p, GitHubProvider)
    p.close()


def test_resolves_github_when_repo_vcs_says_github():
    svc = _service(("github", ""))
    p = svc._create_vcs_provider("o", "r")
    assert isinstance(p, GitHubProvider)
    p.close()
