import pytest

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import BoardProviderRegistry
from reviewer.tasks.boards.runtime import resolved_provider
from tests.tasks.boards.provider_fakes import fake_provider_spec


def test_resolved_provider_selects_single_configured_type_and_closes():
    registry = BoardProviderRegistry([fake_provider_spec()])
    credentials = ProviderCredentialSource(values={"FAKE_TOKEN": "secret"})

    with resolved_provider(
        Settings(_env_file=None),
        None,
        {},
        registry=registry,
        credential_source=credentials,
    ) as resolved:
        provider = resolved.provider
        assert resolved.board_type == "fake"
        assert resolved.secrets == frozenset({"secret"})
        assert provider.closed is False

    assert provider.closed is True


def test_resolved_provider_closes_and_sanitizes_provider_error():
    secret = "runtime-secret"

    def factory(context):
        provider = fake_provider_spec().factory(context)

        def fail(*args, **kwargs):
            raise RuntimeError(f"upstream rejected {secret}")

        provider.list_targets = fail
        return provider

    registry = BoardProviderRegistry([fake_provider_spec(factory=factory)])
    credentials = ProviderCredentialSource(values={"FAKE_TOKEN": secret})

    with pytest.raises(BoardProviderError) as exc_info:
        with resolved_provider(
            Settings(_env_file=None),
            "fake",
            {},
            registry=registry,
            credential_source=credentials,
        ) as resolved:
            provider = resolved.provider
            provider.list_targets(None)

    assert secret not in str(exc_info.value)
    assert provider.closed is True


def test_resolved_provider_sanitizes_runtime_error_from_factory():
    secret = "factory-runtime-secret"

    def factory(context):
        raise RuntimeError(f"factory rejected {context.credentials['FAKE_TOKEN']}")

    registry = BoardProviderRegistry([fake_provider_spec(factory=factory)])
    credentials = ProviderCredentialSource(values={"FAKE_TOKEN": secret})

    with pytest.raises(BoardProviderError) as exc_info:
        with resolved_provider(
            Settings(_env_file=None),
            "fake",
            {},
            registry=registry,
            credential_source=credentials,
        ):
            pytest.fail("factory error must be raised before entering the context")

    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


def test_resolved_provider_resanitizes_board_error_from_factory():
    secret = "factory-board-secret"

    def factory(context):
        value = context.credentials["FAKE_TOKEN"]
        raise BoardProviderError(
            "authentication",
            f"token {value} was rejected",
            hint=f"replace {value}",
            retryable=True,
        )

    registry = BoardProviderRegistry([fake_provider_spec(factory=factory)])
    credentials = ProviderCredentialSource(values={"FAKE_TOKEN": secret})

    with pytest.raises(BoardProviderError) as exc_info:
        with resolved_provider(
            Settings(_env_file=None),
            "fake",
            {},
            registry=registry,
            credential_source=credentials,
        ):
            pytest.fail("factory error must be raised before entering the context")

    error = exc_info.value
    assert error.category == "authentication"
    assert error.retryable is True
    assert secret not in str(error)
    assert secret not in repr(error)
    assert "[REDACTED]" in error.message
    assert "[REDACTED]" in error.hint


def test_resolved_provider_rejects_ambiguous_or_unconfigured_type_safely():
    registry = BoardProviderRegistry([
        fake_provider_spec(board_type="first"),
        fake_provider_spec(board_type="second"),
    ])
    credentials = ProviderCredentialSource(values={"FAKE_TOKEN": "secret"})

    with pytest.raises(BoardProviderError, match="board_type is required"):
        with resolved_provider(
            Settings(_env_file=None),
            None,
            {},
            registry=registry,
            credential_source=credentials,
        ):
            pytest.fail("ambiguous configuration must fail before entering the context")

    with pytest.raises(BoardProviderError, match="not configured"):
        with resolved_provider(
            Settings(_env_file=None),
            "first",
            {},
            registry=registry,
            credential_source=ProviderCredentialSource(values={}),
        ):
            pytest.fail("missing credentials must fail before entering the context")
