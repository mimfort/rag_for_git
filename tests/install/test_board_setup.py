from __future__ import annotations

import json
import logging
from dataclasses import replace

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.jira import provider_spec as jira_provider_spec
from reviewer.tasks.boards.setup import (
    SetupChoice,
    acquire_yougile_key,
    configure_board_provider,
)
from reviewer.tasks.boards.youtrack import provider_spec as youtrack_provider_spec


class FakeIO:
    def __init__(
        self,
        *,
        answers: dict[str, str] | None = None,
        confirms: list[bool] | None = None,
        choice: str = "",
        dry_run: bool = False,
        non_interactive: bool = False,
    ) -> None:
        self.answers = answers or {}
        self.confirms = list(confirms or [])
        self.choice = choice
        self.dry_run = dry_run
        self.non_interactive = non_interactive
        self.prompts: list[tuple[str, bool, str]] = []
        self.opened: list[str] = []
        self.messages: list[str] = []
        self.saved_values: dict[str, str] = {}
        self.save_attempts: list[dict[str, str]] = []
        self.events: list[str] = []

    def confirm(self, text: str, default: bool = False) -> bool:
        self.events.append(f"confirm:{text}")
        self.messages.append(text)
        return self.confirms.pop(0) if self.confirms else default

    def prompt(self, text: str, *, secret: bool = False, default: str = "") -> str:
        self.events.append(f"prompt:{text}")
        self.prompts.append((text, secret, default))
        return self.answers.get(text, default)

    def choose(self, text: str, choices: list[SetupChoice]) -> str:
        self.events.append(f"choose:{text}")
        self.messages.append(text)
        assert self.choice in {choice.value for choice in choices}
        return self.choice

    def open_url(self, url: str) -> None:
        self.events.append(f"open:{url}")
        self.opened.append(url)

    def echo(self, text: str) -> None:
        self.events.append("echo")
        self.messages.append(text)

    def save(self, values: dict[str, str]) -> None:
        attempted = dict(values)
        self.save_attempts.append(attempted)
        self.saved_values.update(attempted)


class FakeProvider:
    board_type = "fake"

    def __init__(self, validation: dict) -> None:
        self.validation = validation
        self.projects: list[str | None] = []
        self.closed = False

    def validate_connection(self, project=None):
        self.projects.append(project)
        return self.validation

    def close(self) -> None:
        self.closed = True


def _hostile_validation_report(secret: str) -> dict:
    return {
        "status": "ok",
        "identity": {
            "id": "user-1",
            "display_name": "Reviewer Bot",
            "name": "Safe identity; Authorization=Basic warning-basic-secret",
            "refresh_token": "nested-refresh-secret",
            "profile": {"api-key": "nested-api-key-secret"},
        },
        "project": {
            "id": "project-1",
            "key": "PRI",
            "name": "Reviewer",
            "authorization": "nested-authorization-secret",
            "nested": {"password": "nested-password-secret"},
        },
        "capabilities": {
            "read": True,
            "create": False,
            "credential": True,
            "token": "nested-capability-secret",
        },
        "warnings": [
            "connection is read-only",
            "refresh_token=warning-refresh-secret",
            "api_token=warning-api-secret",
            "Authorization: Bearer warning-bearer-secret",
            "password: warning-password-secret",
            "visit https://user:url-password-secret@warning.example/help",
            f"known literal {secret}",
            {"cookie": "nested-cookie-secret"},
        ],
        "debug": {"secret": "top-level-secret"},
    }


def test_setup_uses_fail_closed_validation_report_sanitizer() -> None:
    provider = FakeProvider(_hostile_validation_report("jira-secret"))
    spec = replace(jira_provider_spec(), factory=lambda _context: provider)
    io = FakeIO(
        answers={
            "Jira Cloud site URL": "https://acme.atlassian.net",
            "Atlassian account email": "bot@example.test",
            "Atlassian API token": "jira-secret",
        },
        confirms=[False],
    )

    configure_board_provider(spec, io)

    rendered = "\n".join(io.messages)
    for safe in (
        "ok",
        "user-1",
        "Reviewer Bot",
        "Safe identity",
        "project-1",
        "PRI",
        "connection is read-only",
        '"read": true',
        '"create": false',
    ):
        assert safe in rendered
    for forbidden in (
        "nested-refresh-secret",
        "nested-api-key-secret",
        "nested-authorization-secret",
        "nested-password-secret",
        "nested-capability-secret",
        "warning-refresh-secret",
        "warning-api-secret",
        "warning-bearer-secret",
        "warning-basic-secret",
        "warning-password-secret",
        "url-password-secret",
        "jira-secret",
        "nested-cookie-secret",
        "top-level-secret",
    ):
        assert forbidden not in rendered
    assert provider.closed is True


def test_setup_prints_access_contract_before_first_secret_prompt() -> None:
    provider = FakeProvider({"status": "ok", "warnings": []})
    spec = replace(jira_provider_spec(), factory=lambda _context: provider)
    io = FakeIO(
        answers={
            "Jira Cloud site URL": "https://acme.atlassian.net",
            "Atlassian account email": "bot@example.test",
            "Atlassian API token": "jira-secret",
        },
        confirms=[False],
    )

    configure_board_provider(spec, io)

    rendered = "\n".join(io.messages)
    assert "Минимальные права:" in rendered
    assert "Чтение:" in rendered
    assert "Запись:" in rendered
    assert "Проверка:" in rendered
    assert io.events.index("echo") < next(
        index
        for index, event in enumerate(io.events)
        if event == "prompt:Atlassian API token"
    )


def test_jira_setup_opens_official_page_only_after_confirmation_and_validates() -> None:
    provider = FakeProvider(
        {
            "status": "ok",
            "identity": {"display_name": "Reviewer Bot"},
            "project": "PRI",
            "capabilities": {"read": True},
            "warnings": [],
            "debug": "jira-secret",
        }
    )
    contexts = []
    spec = replace(
        jira_provider_spec(),
        factory=lambda context: contexts.append(context) or provider,
    )
    io = FakeIO(
        answers={
            "Jira Cloud site URL": "https://acme.atlassian.net",
            "Atlassian account email": "bot@example.test",
            "Atlassian API token": "jira-secret",
            "Project key for validation (optional)": "PRI",
        },
        confirms=[True],
    )

    result = configure_board_provider(spec, io)

    assert io.opened == ["https://id.atlassian.com/manage-profile/security/api-tokens"]
    assert ("Atlassian account email", True, "") in io.prompts
    assert ("Atlassian API token", True, "") in io.prompts
    assert result == {
        "JIRA_BASE_URL": "https://acme.atlassian.net",
        "JIRA_EMAIL": "bot@example.test",
        "JIRA_API_TOKEN": "jira-secret",
    }
    assert dict(contexts[0].credentials) == result
    assert provider.projects == ["PRI"]
    assert provider.closed is True
    rendered = "\n".join(io.messages)
    assert "token без scopes" in rendered
    assert "Reviewer Bot" in rendered
    assert "debug" not in rendered
    assert "jira-secret" not in rendered


def test_jira_setup_does_not_open_browser_without_confirmation() -> None:
    provider = FakeProvider({"status": "ok", "warnings": []})
    spec = replace(jira_provider_spec(), factory=lambda _context: provider)
    io = FakeIO(
        answers={
            "Jira Cloud site URL": "https://acme.atlassian.net",
            "Atlassian account email": "bot@example.test",
            "Atlassian API token": "jira-secret",
        },
        confirms=[False],
    )

    configure_board_provider(spec, io)

    assert io.opened == []


def test_jira_setup_rejects_rest_api_base_before_network() -> None:
    io = FakeIO(
        answers={
            "Jira Cloud site URL": "https://acme.atlassian.net/rest/api/3",
            "Atlassian account email": "bot@example.test",
            "Atlassian API token": "jira-secret",
        },
        confirms=[False],
    )

    with pytest.raises(BoardProviderError, match="exact Jira Cloud tenant origin"):
        configure_board_provider(jira_provider_spec(), io)

    assert "jira-secret" not in "\n".join(io.messages)


def test_setup_sanitizes_factory_errors_with_collected_secrets() -> None:
    def fail(context):
        token = context.credentials["JIRA_API_TOKEN"]
        raise BoardProviderError(
            "configuration",
            f"invalid token {token}",
            hint=f"replace {token}",
        )

    spec = replace(jira_provider_spec(), factory=fail)
    io = FakeIO(
        answers={
            "Jira Cloud site URL": "https://acme.atlassian.net",
            "Atlassian account email": "bot@example.test",
            "Atlassian API token": "jira-secret",
        },
        confirms=[False],
    )

    with pytest.raises(BoardProviderError) as raised:
        configure_board_provider(spec, io)

    assert "jira-secret" not in f"{raised.value!r} {raised.value}"
    assert "[REDACTED]" in raised.value.message


def test_youtrack_setup_opens_instance_security_page_and_explains_full_token() -> None:
    provider = FakeProvider(
        {
            "status": "ok",
            "identity": {"login": "reviewer"},
            "project": {"key": "PRI", "name": "Reviewer"},
            "capabilities": ["sync"],
            "warnings": [],
        }
    )
    spec = replace(youtrack_provider_spec(), factory=lambda _context: provider)
    io = FakeIO(
        answers={
            "YouTrack API base URL": "https://acme.youtrack.cloud/api",
            "YouTrack permanent token": "perm:full-token",
            "Project key for validation (optional)": "PRI",
        },
        confirms=[True],
    )

    result = configure_board_provider(spec, io)

    assert io.opened == [
        "https://acme.youtrack.cloud/users/me?tab=accountSecurity"
    ]
    assert io.events.index(f"open:{io.opened[0]}") < io.events.index(
        "prompt:YouTrack permanent token"
    )
    assert result == {
        "YOUTRACK_TOKEN": "perm:full-token",
        "YOUTRACK_BASE_URL": "https://acme.youtrack.cloud/api",
    }
    rendered = "\n".join(io.messages)
    assert "YouTrack service scope" in rendered
    assert "perm:" in rendered
    assert "bundled Hub" in rendered
    assert "external Hub" in rendered
    assert "Profile" in rendered
    assert "reviewer" in rendered
    assert "PRI" in rendered
    assert "full-token" not in rendered


def _yougile_client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://yougile.example/api-v2",
        transport=httpx.MockTransport(handler),
    )


def test_yougile_acquires_api_key_from_company_without_saving_password() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/auth/companies"):
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"id": "company-1", "name": "Acme"},
                        {"id": "company-2", "name": "Other"},
                    ],
                    "paging": {"next": False},
                },
            )
        return httpx.Response(201, json={"key": "yougile-api-key"})

    io = FakeIO(
        answers={
            "YouGile API base URL": "https://yougile.example/api-v2",
            "YouGile login": "user@example.test",
            "YouGile password": "never-persist",
        },
        confirms=[True],
        choice="company-1",
    )

    result = acquire_yougile_key(io, client=_yougile_client(handler))

    assert result == {
        "YOUGILE_API_KEY": "yougile-api-key",
        "YOUGILE_API_BASE": "https://yougile.example/api-v2",
    }
    assert ("YouGile login", True, "") in io.prompts
    assert ("YouGile password", True, "") in io.prompts
    assert [path for path, _body in requests] == [
        "/api-v2/auth/companies",
        "/api-v2/auth/keys",
    ]
    assert requests[1][1]["companyId"] == "company-1"
    io.save(result)
    assert io.save_attempts == [result]
    assert "YOUGILE_PASSWORD" not in io.saved_values


def test_yougile_rejects_unsafe_base_before_secret_prompts_or_network() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(500)

    io = FakeIO(
        answers={
            "YouGile API base URL": "http://user:password@yougile.example/api-v2",
            "YouGile login": "user@example.test",
            "YouGile password": "never-persist",
        },
        confirms=[True],
    )

    with pytest.raises(BoardProviderError, match="HTTPS"):
        acquire_yougile_key(io, client=_yougile_client(handler))

    assert requests == []
    assert not any(text in {"YouGile login", "YouGile password"} for text, *_ in io.prompts)
    assert "never-persist" not in "\n".join(io.messages)


def test_yougile_client_initialization_failure_does_not_leak_password(
    monkeypatch,
) -> None:
    io = FakeIO(
        answers={
            "YouGile API base URL": "https://yougile.example/api-v2",
            "YouGile login": "user@example.test",
            "YouGile password": "never-persist",
        },
        confirms=[True],
    )
    monkeypatch.setattr(
        "reviewer.tasks.boards.setup.httpx.Client",
        lambda **_kwargs: (_ for _ in ()).throw(httpx.InvalidURL("never-persist")),
    )

    with pytest.raises(BoardProviderError) as raised:
        acquire_yougile_key(io)

    assert "never-persist" not in f"{raised.value!r} {raised.value}"


def test_yougile_password_is_discarded_on_failure(caplog) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("transport failed")

    io = FakeIO(
        answers={
            "YouGile API base URL": "https://yougile.example/api-v2",
            "YouGile login": "user@example.test",
            "YouGile password": "never-persist",
        },
        confirms=[True, False],
    )

    with pytest.raises(BoardProviderError) as raised:
        acquire_yougile_key(io, client=_yougile_client(handler))

    rendered = (
        repr(io.saved_values)
        + repr(io.save_attempts)
        + repr(raised.value)
        + str(raised.value)
        + caplog.text
        + "\n".join(io.messages)
    )
    assert "never-persist" not in rendered
    assert "YOUGILE_PASSWORD" not in io.saved_values
    assert all("YOUGILE_PASSWORD" not in attempt for attempt in io.save_attempts)


def test_yougile_manual_key_fallback_uses_hidden_prompt() -> None:
    io = FakeIO(
        answers={
            "YouGile API base URL": "https://yougile.example/api-v2",
            "YouGile API key": "manual-key",
        },
        confirms=[False],
    )

    result = acquire_yougile_key(io)

    assert result["YOUGILE_API_KEY"] == "manual-key"
    assert ("YouGile API key", True, "") in io.prompts


def test_yougile_openid_only_limitation_never_attempts_oauth_exchange() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(403, json={"error": "allowOnlyOpenId is enabled"})

    io = FakeIO(
        answers={
            "YouGile API base URL": "https://yougile.example/api-v2",
            "YouGile login": "user@example.test",
            "YouGile password": "never-persist",
            "YouGile API key": "manual-key",
        },
        confirms=[True, True],
    )

    result = acquire_yougile_key(io, client=_yougile_client(handler))

    assert result["YOUGILE_API_KEY"] == "manual-key"
    assert paths == ["/api-v2/auth/companies"]
    rendered = "\n".join(io.messages)
    assert "allowOnlyOpenId" in rendered
    assert "API-capable account" in rendered
    assert "OAuth exchange" not in rendered


def test_noninteractive_setup_never_calls_acquisition_or_validation() -> None:
    calls: list[str] = []
    spec = replace(
        jira_provider_spec(),
        setup=replace(
            jira_provider_spec().setup,
            acquisition=lambda _io: calls.append("acquisition") or {},
        ),
        factory=lambda _context: calls.append("factory") or FakeProvider({}),
    )

    assert configure_board_provider(spec, FakeIO(non_interactive=True)) == {}
    assert configure_board_provider(spec, FakeIO(dry_run=True)) == {}
    assert calls == []
