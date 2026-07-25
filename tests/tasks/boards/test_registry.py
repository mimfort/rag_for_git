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


EXPECTED_BOARD_TYPES = (
    "yougile",
    "youtrack",
    "jira",
    "github",
    "trello",
    "linear",
    "clickup",
    "asana",
    "yandex_tracker",
    "kaiten",
    "weeek",
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
        return None


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


def test_registry_rejects_secret_embedded_in_nested_option_text():
    registry = BoardProviderRegistry([
        fake_spec(options=(ProviderOptionSpec("config", "Config"),)),
    ])
    secret = "long-server-secret"

    with pytest.raises(ValueError, match="secret value") as exc_info:
        registry.create(
            "fake",
            credentials={"FAKE_TOKEN": secret},
            options={"config": {"authorization": f"Bearer {secret}"}},
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


# Поля, форма которых строже «просто https-URL»: tenant-origin Jira и ровно один
# из двух взаимоисключающих org-заголовков Yandex Tracker.
CREDENTIAL_OVERRIDES = {
    "JIRA_BASE_URL": "https://acme.atlassian.net",
    "YANDEX_TRACKER_ORG_ID": "org-42",
}


def _dummy_credential(env: str) -> str:
    """Синтетическое значение credential нужной формы: URL, email или произвольный секрет."""
    if env in CREDENTIAL_OVERRIDES:
        return CREDENTIAL_OVERRIDES[env]
    if env.endswith(("_BASE", "_URL")) or "BASE_URL" in env:
        return "https://board.example.test"
    if "EMAIL" in env:
        return "bot@example.test"
    return "dummy-credential-value"


def test_default_registry_registers_every_complete_provider_in_order():
    registry = default_board_registry()

    assert registry.registered_types() == EXPECTED_BOARD_TYPES


def test_default_registry_builds_and_validates_every_registered_provider():
    """Каждый зарегистрированный тип проходит `_validate_runtime_provider` при создании."""
    registry = default_board_registry()

    for board_type in registry.registered_types():
        spec = registry.get(board_type)
        # Как и `ProviderCredentialSource`, реестру передаются все объявленные поля:
        # обязательные — синтетическим значением, необязательные — своим дефолтом.
        credentials = {
            field.env: (
                CREDENTIAL_OVERRIDES[field.env]
                if field.env in CREDENTIAL_OVERRIDES
                else (_dummy_credential(field.env) if field.required else field.default)
            )
            for field in spec.credential_fields
        }
        provider = registry.create(
            board_type,
            credentials=credentials,
            options={},
            build_defaults=BUILD_DEFAULTS,
        )
        try:
            assert provider.board_type == board_type
            assert spec.setup.label
            assert spec.create_target_label and spec.done_target_label
        finally:
            provider.close()


def test_every_registered_spec_rejects_a_secret_smuggled_through_options():
    """`_contains_secret` проверяется на каждой зарегистрированной spec, не только на фейковой."""
    registry = default_board_registry()
    checked = []

    for board_type in registry.registered_types():
        spec = registry.get(board_type)
        secret_fields = [field for field in spec.credential_fields if field.secret]
        if not spec.option_fields or not secret_fields:
            continue
        secret = f"server-secret-for-{board_type}"
        credentials = {
            field.env: (
                secret
                if field.secret
                else (
                    CREDENTIAL_OVERRIDES.get(field.env)
                    or (_dummy_credential(field.env) if field.required else field.default)
                )
            )
            for field in spec.credential_fields
        }
        option_key = spec.option_fields[0].key

        with pytest.raises(ValueError, match="secret value") as exc_info:
            registry.create(
                board_type,
                credentials=credentials,
                options={option_key: secret},
                build_defaults=BUILD_DEFAULTS,
            )
        assert secret not in str(exc_info.value), board_type
        checked.append(board_type)

    # Типы без опций или без секретных полей проверять нечем; остальные обязаны быть покрыты.
    expected = [
        board_type
        for board_type in registry.registered_types()
        if registry.get(board_type).option_fields
        and any(field.secret for field in registry.get(board_type).credential_fields)
    ]
    assert checked == expected


def test_default_registry_exposes_jira_credential_schema_and_builds_provider():
    registry = default_board_registry()

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
