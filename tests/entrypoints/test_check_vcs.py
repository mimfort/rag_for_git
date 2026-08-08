import httpx

from reviewer.config.settings import Settings
from reviewer.entrypoints.cli import _check_vcs_providers


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_check_accepts_gitlab_only_and_uses_configured_base(monkeypatch, capsys):
    seen = []

    def get(url, **kwargs):
        seen.append((url, kwargs["headers"]))
        return _Response(200, {"username": "bot"})

    monkeypatch.setattr(httpx, "get", get)
    failed = _check_vcs_providers(
        Settings(
            _env_file=None,
            github_token="",
            gitlab_token="gl-secret",
            gitlab_url="https://gitlab.example",
        )
    )
    output = capsys.readouterr().out

    assert failed is False
    assert seen == [
        (
            "https://gitlab.example/api/v4/user",
            {"PRIVATE-TOKEN": "gl-secret"},
        )
    ]
    assert "GitLab API: аутентификация OK" in output
    assert "gl-secret" not in output


def test_check_requires_at_least_one_vcs_token(capsys):
    failed = _check_vcs_providers(
        Settings(
            _env_file=None,
            github_token="",
            gitlab_token="",
        )
    )
    output = capsys.readouterr().out

    assert failed is True
    assert "не настроен ни один VCS token" in output


def test_check_validates_both_configured_tokens(monkeypatch, capsys):
    urls = []

    def get(url, **_kwargs):
        urls.append(url)
        return _Response(200, {"login": "gh", "username": "gl"})

    monkeypatch.setattr(httpx, "get", get)
    failed = _check_vcs_providers(
        Settings(
            _env_file=None,
            github_token="gh-secret",
            gitlab_token="gl-secret",
        )
    )
    output = capsys.readouterr().out

    assert failed is False
    assert urls == [
        "https://api.github.com/user",
        "https://gitlab.com/api/v4/user",
    ]
    assert "GitHub API: аутентификация OK" in output
    assert "GitLab API: аутентификация OK" in output


def test_check_vcs_failure_does_not_echo_secret(monkeypatch, capsys):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_args, **_kwargs: _Response(401, {"token": "gh-secret"}),
    )

    failed = _check_vcs_providers(
        Settings(_env_file=None, github_token="gh-secret", gitlab_token="")
    )
    output = capsys.readouterr().out

    assert failed is True
    assert "GitHub API: HTTP 401" in output
    assert "gh-secret" not in output
