"""Общий contract-набор на фейке linear + проверки registry-спеки провайдера.

Постоянная параметризация фейка в ``contract.py`` — фаза C; здесь набор запускается
локально, без правки общего файла.
"""
from __future__ import annotations

import pytest

from reviewer.tasks.boards.linear import DEFAULT_API_BASE, LinearBoard, provider_spec
from reviewer.tasks.boards.registry import BoardProviderRegistry
from tests.tasks.boards.contract import ProviderContract
from tests.tasks.boards.fakes import linear as fake


@pytest.mark.parametrize("adapter", [fake.ADAPTER], indirect=True)
class TestLinearContract(ProviderContract):
    pass


def test_spec_declares_credentials_options_and_labels() -> None:
    spec = provider_spec()

    assert spec.board_type == "linear"
    assert [(item.env, item.secret, item.required, item.default) for item in
            spec.credential_fields] == [
        ("LINEAR_API_KEY", True, True, ""),
        ("LINEAR_API_BASE", False, False, DEFAULT_API_BASE),
    ]
    assert [(item.key, item.required_for) for item in spec.option_fields] == [
        ("team_key", ("sync", "create"))
    ]
    assert spec.default_api_base == DEFAULT_API_BASE
    assert spec.setup.help_url == "https://linear.app/settings/account/security"
    assert spec.setup.help_text
    assert (spec.create_target_label, spec.done_target_label) == (
        "Workflow state создания",
        "Workflow state завершения",
    )


def test_registry_builds_provider_from_spec() -> None:
    registry = BoardProviderRegistry([provider_spec()])
    provider = registry.create(
        "linear",
        credentials={"LINEAR_API_KEY": "linear-key", "LINEAR_API_BASE": ""},
        options={"team_key": "ENG"},
        build_defaults={
            "key_pattern": r"ENG-\d+",
            "url_template": "https://linear.app/acme/issue/{code}",
            "attachment_max_bytes": 1000,
            "attachment_timeout": 1.0,
            "attachment_store_chars": 1000,
        },
    )
    try:
        assert isinstance(provider, LinearBoard)
        assert provider.board_type == "linear"
        assert provider.secrets == frozenset({"linear-key"})
    finally:
        provider.close()


def test_registry_rejects_secret_passed_as_option() -> None:
    registry = BoardProviderRegistry([provider_spec()])

    with pytest.raises(ValueError):
        registry.create(
            "linear",
            credentials={"LINEAR_API_KEY": "linear-key-value", "LINEAR_API_BASE": ""},
            options={"team_key": "linear-key-value"},
            build_defaults={
                "key_pattern": "",
                "url_template": "",
                "attachment_max_bytes": 1,
                "attachment_timeout": 1.0,
                "attachment_store_chars": 1,
            },
        )
