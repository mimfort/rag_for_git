"""Общий contract-набор на фейке github (постоянная параметризация — в фазе C)."""
from __future__ import annotations

import pytest

from reviewer.tasks.boards.github import provider_spec
from reviewer.tasks.boards.registry import BoardProviderRegistry
from tests.tasks.boards.contract import ProviderContract
from tests.tasks.boards.fakes import github as fake

_BUILD_DEFAULTS = {
    "key_pattern": r"PRI-\d+",
    "url_template": "",
    "attachment_max_bytes": 1000,
    "attachment_timeout": 1.0,
    "attachment_store_chars": 1000,
}


@pytest.mark.parametrize("adapter", [fake.ADAPTER], indirect=True)
class TestGitHubContract(ProviderContract):
    pass


def test_spec_declares_credentials_options_and_labels() -> None:
    spec = provider_spec()

    assert spec.board_type == "github"
    assert [(field.env, field.secret, field.required, field.default, field.aliases)
            for field in spec.credential_fields] == [
        ("GITHUB_ISSUES_TOKEN", True, True, "", ()),
        ("GITHUB_ISSUES_API_BASE", False, False, "https://api.github.com", ()),
    ]
    assert [(option.key, option.required_for) for option in spec.option_fields] == [
        ("repo", ("sync", "create", "finish")),
        ("key_prefix", ("sync",)),
    ]
    assert spec.default_api_base == "https://api.github.com"
    assert spec.create_target_label == "Метка или milestone создания"
    assert spec.done_target_label == "Метка или milestone закрытия"


def test_help_url_builder_follows_the_configured_api_base() -> None:
    spec = provider_spec()
    builder = spec.setup.help_url_builder
    assert builder is not None

    assert spec.setup.help_url == "https://github.com/settings/personal-access-tokens/new"
    assert builder({}) == spec.setup.help_url
    assert builder({"GITHUB_ISSUES_API_BASE": "https://ghe.example/api/v3"}) == (
        "https://ghe.example/settings/personal-access-tokens/new"
    )
    assert builder({"GITHUB_ISSUES_API_BASE": "ftp://broken"}) == spec.setup.help_url


def test_registry_accepts_the_spec_and_builds_a_full_provider() -> None:
    registry = BoardProviderRegistry([provider_spec()])

    provider = registry.create(
        "github",
        credentials={
            "GITHUB_ISSUES_TOKEN": fake.SECRET,
            "GITHUB_ISSUES_API_BASE": "https://api.github.com",
        },
        options={"repo": "acme/widgets", "key_prefix": "PRI"},
        build_defaults=_BUILD_DEFAULTS,
    )

    assert provider.board_type == "github"
    assert fake.SECRET in provider.secrets
    provider.close()
