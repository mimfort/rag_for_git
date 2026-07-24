from __future__ import annotations

from types import SimpleNamespace

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.entrypoints.cli import _check_board_providers
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderSetupSpec,
)


class CheckProvider:
    board_type = "fake"

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.closed = False

    def validate_connection(self, project=None):
        assert project is None
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
