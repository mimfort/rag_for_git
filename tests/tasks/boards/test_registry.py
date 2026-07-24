from __future__ import annotations

from dataclasses import replace

import pytest

from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
    default_board_registry,
)


BUILD_DEFAULTS = {
    "key_pattern": r"[A-Z]+-\\d+",
    "url_template": "https://board.example/{key}",
    "attachment_max_bytes": 100,
    "attachment_timeout": 1.0,
    "attachment_store_chars": 100,
}


class _CompleteProvider:
    board_type = "fake"

    def validate_connection(self, project=None):
        return {}

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
        pass


def fake_spec(*, board_type="fake", secret_env="FAKE_TOKEN", factory=None, options=()):
    return BoardProviderSpec(
        board_type=board_type,
        factory=factory or (lambda _: _CompleteProvider()),
        credential_fields=(CredentialFieldSpec(secret_env, "Token", secret=True),),
        setup=ProviderSetupSpec("Fake", "https://example.test/help", "Create a token."),
        option_fields=options,
    )


def test_registry_preserves_registration_order_and_rejects_duplicate_or_empty_type():
    registry = BoardProviderRegistry()
    registry.register(fake_spec(board_type="first"))
    registry.register(fake_spec(board_type="second"))

    assert registry.registered_types() == ("first", "second")
    assert registry.get("second").board_type == "second"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(fake_spec(board_type="first"))
    with pytest.raises(ValueError, match="board_type"):
        registry.register(fake_spec(board_type="  "))


def test_registry_rejects_incompatible_repeated_credential_declarations():
    registry = BoardProviderRegistry()
    registry.register(fake_spec(board_type="one", secret_env="SHARED_TOKEN"))
    incompatible = replace(
        fake_spec(board_type="two", secret_env="SHARED_TOKEN"),
        credential_fields=(CredentialFieldSpec("SHARED_TOKEN", "Other", secret=False),),
    )

    with pytest.raises(ValueError, match="incompatible credential declaration"):
        registry.register(incompatible)


def test_registry_rejects_unknown_provider_option():
    registry = BoardProviderRegistry([fake_spec(options=(ProviderOptionSpec("project", "Project"),))])

    with pytest.raises(ValueError, match="unknown provider option") as exc_info:
        registry.create(
            "fake", credentials={"FAKE_TOKEN": "secret"}, options={"private-option": "x"},
            build_defaults=BUILD_DEFAULTS,
        )
    assert "private-option" not in str(exc_info.value)


def test_registry_rejects_secret_option_and_incomplete_provider():
    registry = BoardProviderRegistry()
    registry.register(fake_spec(secret_env="FAKE_TOKEN"))
    with pytest.raises(ValueError, match="credentials must not be provider options") as exc_info:
        registry.create(
            "fake",
            credentials={"FAKE_TOKEN": "secret"},
            options={"FAKE_TOKEN": "secret"},
            build_defaults=BUILD_DEFAULTS,
        )
    assert "FAKE_TOKEN" not in str(exc_info.value)
    registry.register(fake_spec(factory=lambda _: object(), board_type="broken"))
    with pytest.raises(TypeError, match="validate_connection"):
        registry.create(
            "broken",
            credentials={"FAKE_TOKEN": "x"},
            options={},
            build_defaults=BUILD_DEFAULTS,
        )


def test_registry_rejects_nested_secret_values_without_echoing_them():
    registry = BoardProviderRegistry([
        fake_spec(options=(ProviderOptionSpec("config", "Config"),)),
    ])
    secret = "nested-secret-value"

    with pytest.raises(ValueError, match="secret value") as exc_info:
        registry.create(
            "fake",
            credentials={"FAKE_TOKEN": secret},
            options={"config": {"nested": ["safe", {"token": secret}]}},
            build_defaults=BUILD_DEFAULTS,
        )

    assert secret not in str(exc_info.value)


def test_registry_closes_incomplete_provider_rejected_at_runtime():
    class _IncompleteProvider:
        board_type = "broken"

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    provider = _IncompleteProvider()
    registry = BoardProviderRegistry([
        fake_spec(board_type="broken", factory=lambda _: provider),
    ])

    with pytest.raises(TypeError, match="validate_connection"):
        registry.create(
            "broken",
            credentials={"FAKE_TOKEN": "secret"},
            options={},
            build_defaults=BUILD_DEFAULTS,
        )

    assert provider.closed is True


def test_registry_rejects_provider_with_a_different_runtime_board_type():
    class _MismatchedProvider(_CompleteProvider):
        board_type = "other"

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    provider = _MismatchedProvider()

    registry = BoardProviderRegistry([
        fake_spec(board_type="fake", factory=lambda _: provider),
    ])

    with pytest.raises(TypeError, match="does not match registered board_type"):
        registry.create(
            "fake", credentials={"FAKE_TOKEN": "secret"}, options={}, build_defaults=BUILD_DEFAULTS,
        )
    assert provider.closed is True


def test_registry_creates_provider_with_validated_context():
    contexts: list[ProviderBuildContext] = []

    def factory(context):
        contexts.append(context)
        return _CompleteProvider()

    registry = BoardProviderRegistry([
        fake_spec(options=(ProviderOptionSpec("project", "Project"),), factory=factory),
    ])

    provider = registry.create(
        "fake", credentials={"FAKE_TOKEN": "secret"}, options={"project": "A"},
        build_defaults=BUILD_DEFAULTS,
    )

    assert isinstance(provider, _CompleteProvider)
    assert contexts[0].credentials == {"FAKE_TOKEN": "secret"}
    assert contexts[0].options == {"project": "A"}
    assert contexts[0].key_pattern == BUILD_DEFAULTS["key_pattern"]


def test_default_registry_registers_jira_only_as_complete_provider():
    registry = default_board_registry()

    assert registry.registered_types() == ("yougile", "youtrack", "jira")
    spec = registry.get("jira")
    assert [field.env for field in spec.credential_fields] == [
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
    ]
    provider = registry.create(
        "jira",
        credentials={
            "JIRA_BASE_URL": "https://acme.atlassian.net",
            "JIRA_EMAIL": "bot@example.test",
            "JIRA_API_TOKEN": "secret",
        },
        options={"issue_type": "10001"},
        build_defaults=BUILD_DEFAULTS,
    )
    provider.close()
