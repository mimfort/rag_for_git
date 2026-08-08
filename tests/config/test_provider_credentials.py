from __future__ import annotations

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderSetupSpec,
)
from tests.provider_access import FAKE_PROVIDER_ACCESS


def _provider(context):
    return context


YOUGILE_SPEC = BoardProviderSpec(
    board_type="yougile",
    factory=_provider,
    credential_fields=(
        CredentialFieldSpec(
            "YOUGILE_API_KEY", "API key", secret=True, aliases=("TASK_BOARD_API_KEY",),
        ),
        CredentialFieldSpec(
            "YOUGILE_API_BASE", "API URL", required=False,
            default="https://yougile.example/api", aliases=("TASK_BOARD_API_BASE",),
        ),
    ),
    setup=ProviderSetupSpec(
        "YouGile", "https://example.test", "Use a token.", FAKE_PROVIDER_ACCESS
    ),
)


def test_process_env_wins_and_legacy_alias_is_supported(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TASK_BOARD_API_KEY=legacy-file\n", encoding="utf-8")
    monkeypatch.setenv("YOUGILE_API_KEY", "process-value")
    source = ProviderCredentialSource(env_file=env_file)

    resolved = source.resolve(YOUGILE_SPEC)

    assert resolved.values["YOUGILE_API_KEY"] == "process-value"
    assert resolved.safe_metadata == {"configured": True, "missing": []}
    assert "process-value" not in repr(resolved.safe_metadata)


def test_required_fields_and_defaults_are_resolved_without_exposing_secrets(tmp_path):
    source = ProviderCredentialSource(env_file=tmp_path / ".env")

    resolved = source.resolve(YOUGILE_SPEC)

    assert resolved.safe_metadata == {"configured": False, "missing": ["YOUGILE_API_KEY"]}
    assert resolved.values["YOUGILE_API_BASE"] == "https://yougile.example/api"
    assert "YOUGILE_API_BASE" not in resolved.safe_metadata


def test_configured_types_and_secret_values_are_stable(tmp_path):
    source = ProviderCredentialSource(
        env_file=tmp_path / ".env", values={"TASK_BOARD_API_KEY": "legacy-secret"},
    )
    registry = BoardProviderRegistry([YOUGILE_SPEC])

    assert source.is_configured(YOUGILE_SPEC) is True
    assert source.configured_types(registry) == ("yougile",)
    assert source.secret_values(YOUGILE_SPEC) == frozenset({"legacy-secret"})


def test_settings_empty_override_suppresses_declared_value_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("YOUGILE_API_KEY=file-secret\n", encoding="utf-8")

    class _Settings:
        _provider_env_file = env_file

        @staticmethod
        def model_dump():
            return {"yougile_api_key": ""}

    source = ProviderCredentialSource.from_settings(_Settings())

    assert source.is_configured(YOUGILE_SPEC) is False
