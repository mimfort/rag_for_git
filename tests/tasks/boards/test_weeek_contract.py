"""Общий contract-набор на фейке weeek + spec/registry-инварианты адаптера."""
from __future__ import annotations

import pytest

from reviewer.tasks.boards.registry import BoardProviderRegistry
from reviewer.tasks.boards.weeek import provider_spec
from tests.tasks.boards.contract import ProviderContract
from tests.tasks.boards.fakes import weeek as fake

_BUILD_DEFAULTS = {
    "key_pattern": r"WEEEK-\d+",
    "url_template": "https://app.weeek.net/ws/1/task/{code}",
    "attachment_max_bytes": 1000,
    "attachment_timeout": 1.0,
    "attachment_store_chars": 1000,
}


@pytest.mark.parametrize("adapter", [fake.ADAPTER], indirect=True)
class TestWeeekContract(ProviderContract):
    pass


def test_spec_declares_credentials_options_and_labels() -> None:
    spec = provider_spec()

    assert spec.board_type == "weeek"
    assert [(field.env, field.secret, field.required, field.default, field.aliases)
            for field in spec.credential_fields] == [
        ("WEEEK_API_TOKEN", True, True, "", ()),
        ("WEEEK_API_BASE", False, False, "https://api.weeek.net/public/v1", ()),
    ]
    assert [(option.key, option.required_for) for option in spec.option_fields] == [
        ("project_id", ("sync", "create", "finish")),
        ("board_id", ("sync", "create", "finish")),
        ("key_prefix", ("sync",)),
    ]
    assert spec.default_api_base == "https://api.weeek.net/public/v1"
    assert spec.create_target_label == "Колонка создания"
    assert spec.done_target_label == "Колонка завершения"
    assert spec.setup.help_url == "https://developers.weeek.net/#generating-access-token"
    assert "token" in spec.setup.help_text


def test_no_secret_is_declared_as_a_provider_option() -> None:
    spec = provider_spec()

    secret_envs = {field.env for field in spec.credential_fields if field.secret}
    assert not secret_envs & {option.key.upper() for option in spec.option_fields}


def test_registry_accepts_the_spec_and_builds_a_full_provider() -> None:
    registry = BoardProviderRegistry([provider_spec()])

    provider = registry.create(
        "weeek",
        credentials={
            "WEEEK_API_TOKEN": fake.SECRET,
            "WEEEK_API_BASE": "https://api.weeek.net/public/v1",
        },
        options={"project_id": "4", "board_id": "6", "key_prefix": "WEEEK"},
        build_defaults=_BUILD_DEFAULTS,
    )

    assert provider.board_type == "weeek"
    assert fake.SECRET in provider.secrets
    assert fake.SECRET not in repr(provider)
    provider.close()
