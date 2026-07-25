from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.entrypoints.cli import _check_board_providers, check
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderSetupSpec,
)


class CheckProvider:
    board_type = "fake"

    def __init__(
        self,
        result=None,
        error: Exception | None = None,
        expected_project: str | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.expected_project = expected_project
        self.closed = False

    def validate_connection(self, project=None):
        assert project == self.expected_project
        if self.error:
            raise self.error
        return self.result

    def iter_raw(self, board, limit):
        return []

    def normalize(self, raw):
        return {}

    def normalize_meta(self, raw):
        return {}

    def fetch_one(self, key):
        return None

    def list_targets(self, project):
        return {}

    def create(self, doc_md, *, title, target, project):
        return {}

    def finish(self, key, pr_url, *, note=None, mark_done=True, target=None):
        return {}

    def close(self):
        self.closed = True


def _settings():
    return SimpleNamespace(
        task_board_key_pattern="",
        task_board_url_template="",
        task_attachment_max_bytes=100,
        task_attachment_timeout=1.0,
        task_attachment_store_chars=1000,
    )


def _registry(provider: CheckProvider) -> BoardProviderRegistry:
    return BoardProviderRegistry(
        [
            BoardProviderSpec(
                board_type="fake",
                factory=lambda _context: provider,
                credential_fields=(
                    CredentialFieldSpec("FAKE_TOKEN", "Token", secret=True),
                ),
                setup=ProviderSetupSpec(
                    "Fake",
                    "https://fake.example/setup",
                    "Create a token.",
                ),
            )
        ]
    )


def test_check_validates_configured_provider_and_renders_safe_metadata(capsys) -> None:
    secret = "fake-secret"
    provider = CheckProvider(
        {
            "status": "ok",
            "identity": {"login": "reviewer"},
            "project": {"key": "PRI"},
            "capabilities": {"read": True, "create": False},
            "warnings": ["create is unavailable"],
            "malicious": secret,
        }
    )

    failed = _check_board_providers(
        _settings(),
        registry=_registry(provider),
        credential_source=ProviderCredentialSource(values={"FAKE_TOKEN": secret}),
    )

    output = capsys.readouterr().out
    assert failed is False
    assert "fake" in output
    assert "reviewer" in output
    assert "PRI" in output
    assert "read" in output
    assert "create is unavailable" in output
    assert secret not in output
    assert "malicious" not in output
    assert provider.closed is True


def test_check_sanitizes_provider_error_and_closes_provider(capsys) -> None:
    secret = "fake-secret"
    provider = CheckProvider(
        error=BoardProviderError(
            "authentication",
            f"request rejected {secret}",
            hint=f"replace {secret}",
        )
    )

    failed = _check_board_providers(
        _settings(),
        registry=_registry(provider),
        credential_source=ProviderCredentialSource(values={"FAKE_TOKEN": secret}),
    )

    output = capsys.readouterr().out
    assert failed is True
    assert "authentication" in output
    assert secret not in output
    assert "[REDACTED]" in output
    assert provider.closed is True


def test_check_recursively_allowlists_validation_report_schema(capsys) -> None:
    provider = CheckProvider(
        {
            "status": "ok",
            "identity": {
                "login": "reviewer",
                "name": "https://user:identity-password@identity.example",
                "details": {"refresh_token": "nested-identity-token"},
            },
            "project": {
                "key": "PRI",
                "metadata": {"refresh_token": "nested-project-token"},
            },
            "capabilities": {
                "read": True,
                "create": "yes",
                "refresh_token": "nested-capability-token",
                "details": {"token": "nested"},
            },
            "warnings": [
                "read-only connection",
                "see https://user:warning-password@warning.example/help",
                {"refresh_token": "nested-warning-token"},
            ],
        }
    )

    failed = _check_board_providers(
        _settings(),
        registry=_registry(provider),
        credential_source=ProviderCredentialSource(values={"FAKE_TOKEN": "configured-secret"}),
    )

    output = capsys.readouterr().out
    assert failed is False
    assert "reviewer" in output
    assert "PRI" in output
    assert '"read": true' in output
    assert "read-only connection" in output
    for forbidden in (
        "refresh_token",
        "nested-identity-token",
        "nested-project-token",
        "nested-capability-token",
        "nested-warning-token",
        "identity-password",
        "warning-password",
        '"create": "yes"',
    ):
        assert forbidden not in output
    assert provider.closed is True


def test_check_uses_fail_closed_validation_report_sanitizer(capsys) -> None:
    provider = CheckProvider(
        {
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
                "known literal configured-secret",
                {"cookie": "nested-cookie-secret"},
            ],
            "debug": {"secret": "top-level-secret"},
        }
    )

    failed = _check_board_providers(
        _settings(),
        registry=_registry(provider),
        credential_source=ProviderCredentialSource(
            values={"FAKE_TOKEN": "configured-secret"}
        ),
    )

    output = capsys.readouterr().out
    assert failed is False
    for safe in (
        "user-1",
        "Reviewer Bot",
        "Safe identity",
        "project-1",
        "PRI",
        "connection is read-only",
        '"read": true',
        '"create": false',
    ):
        assert safe in output
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
        "configured-secret",
        "nested-cookie-secret",
        "top-level-secret",
    ):
        assert forbidden not in output
    assert provider.closed is True


def test_check_skips_unconfigured_provider_without_constructing_it(capsys) -> None:
    provider = CheckProvider({})

    failed = _check_board_providers(
        _settings(),
        registry=_registry(provider),
        credential_source=ProviderCredentialSource(values={}),
    )

    assert failed is False
    assert provider.closed is False
    assert "не настроены" in capsys.readouterr().out


def test_check_passes_explicit_project_to_selected_provider(capsys) -> None:
    provider = CheckProvider(
        {
            "status": "ok",
            "project": {"key": "PRI"},
            "capabilities": {"read": True, "create": True, "transition": True},
            "warnings": [],
        },
        expected_project="PRI",
    )

    failed = _check_board_providers(
        _settings(),
        registry=_registry(provider),
        credential_source=ProviderCredentialSource(values={"FAKE_TOKEN": "secret"}),
        board_projects={"fake": "PRI"},
    )

    assert failed is False
    assert '"key": "PRI"' in capsys.readouterr().out


def test_check_rejects_project_for_unconfigured_provider(capsys) -> None:
    provider = CheckProvider({"status": "ok", "warnings": []})

    failed = _check_board_providers(
        _settings(),
        registry=_registry(provider),
        credential_source=ProviderCredentialSource(values={"FAKE_TOKEN": "secret"}),
        board_projects={"typo": "PRI"},
    )

    assert failed is True
    assert "typo" in capsys.readouterr().out


def test_check_accepts_board_project_for_every_registered_type(capsys) -> None:
    """`--board-project TYPE=PROJECT` работает для каждого зарегистрированного типа."""
    from reviewer.tasks.boards.registry import default_board_registry

    registry = default_board_registry()

    for board_type in registry.registered_types():
        failed = _check_board_providers(
            _settings(),
            registry=registry,
            credential_source=ProviderCredentialSource(values={}),
            board_projects={board_type: "PRI"},
        )

        # Провайдер без кредов не собирается вовсе, но тип распознан и назван в отчёте.
        assert failed is True, board_type
        out = capsys.readouterr().out
        assert board_type in out, board_type
        assert "не настроен" in out, board_type


def test_check_help_documents_repeatable_board_project_option() -> None:
    result = CliRunner().invoke(check, ["--help"])

    assert result.exit_code == 0
    assert "--board-project TYPE=PROJECT" in result.output
