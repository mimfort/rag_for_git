"""Discovery целей Trello: списки доски как create/done-цели плюс registry spec."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import BoardProviderRegistry
from reviewer.tasks.boards.trello import DEFAULT_API_BASE, TrelloBoard, provider_spec

API_KEY = "trello-app-key"
TOKEN = "trello-secret-token"
BOARD = "5f00000000000000000000b0"
BACKLOG = "5f00000000000000000000l1"
DONE = "5f00000000000000000000l2"


def _board(handler, **kwargs) -> TrelloBoard:
    return TrelloBoard(
        api_key=API_KEY,
        api_token=TOKEN,
        board_id=kwargs.pop("board_id", BOARD),
        key_prefix="TRL",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def test_board_lists_become_create_and_done_targets() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {"id": BACKLOG, "name": "Backlog"},
                {"id": DONE, "name": "Done"},
            ],
        )

    board = _board(handler)
    result = board.list_targets("TRL")

    assert set(result) == {"targets", "options", "warnings"}
    assert result["targets"] == [
        {"id": BACKLOG, "label": "Backlog", "purposes": ["create", "done"]},
        {"id": DONE, "label": "Done", "purposes": ["create", "done"]},
    ]
    assert result["options"] == []
    assert result["warnings"] == []
    assert requests[0].url.path == f"/1/boards/{BOARD}/lists"
    assert dict(requests[0].url.params) == {
        "key": API_KEY,
        "token": TOKEN,
        "fields": "id,name",
    }

    board.list_targets("TRL")
    assert len(requests) == 1, "списки доски кэшируются на время жизни провайдера"


def test_missing_board_id_yields_warning_instead_of_raising() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    result = _board(handler, board_id="").list_targets("TRL")

    assert result["targets"] == []
    assert result["warnings"] and "board_id" in result["warnings"][0]
    assert requests == []


def test_board_without_lists_reports_a_warning() -> None:
    result = _board(lambda _: httpx.Response(200, json=[])).list_targets("TRL")

    assert result["targets"] == []
    assert result["warnings"] == ["у доски Trello нет списков"]


def test_validate_connection_reports_identity_capabilities_and_no_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1/members/me":
            return httpx.Response(
                200, json={"id": "u-1", "username": "robot", "fullName": "Робот"}
            )
        if request.url.path == f"/1/boards/{BOARD}":
            return httpx.Response(200, json={"id": BOARD, "name": "Доска", "closed": False})
        return httpx.Response(200, json=[{"id": DONE, "name": "Done"}])

    result = _board(handler).validate_connection("TRL")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["identity"] == {"id": "u-1", "username": "robot", "full_name": "Робот"}
    assert result["capabilities"] == {"read": True, "create": True, "done": True}
    assert result["project"] == "TRL"
    assert result["warnings"] == []
    assert TOKEN not in repr(result) and API_KEY not in repr(result)


def test_provider_spec_declares_secret_credentials_and_board_options() -> None:
    spec = provider_spec()

    assert spec.board_type == "trello"
    assert spec.default_api_base == DEFAULT_API_BASE
    assert [(field.env, field.secret, field.required) for field in spec.credential_fields] == [
        ("TRELLO_API_KEY", True, True),
        ("TRELLO_API_TOKEN", True, True),
        ("TRELLO_API_BASE", False, False),
    ]
    assert [(option.key, option.required_for) for option in spec.option_fields] == [
        ("board_id", ("sync", "create", "finish")),
        ("key_prefix", ("sync",)),
    ]
    assert spec.setup.help_url.startswith("https://")


def test_registry_builds_the_provider_from_credentials_and_options() -> None:
    registry = BoardProviderRegistry([provider_spec()])
    provider = registry.create(
        "trello",
        credentials={
            "TRELLO_API_KEY": API_KEY,
            "TRELLO_API_TOKEN": TOKEN,
            "TRELLO_API_BASE": "",
        },
        options={"board_id": BOARD, "key_prefix": "TRL"},
        build_defaults={
            "key_pattern": r"TRL-\d+",
            "url_template": "https://trello.com/c/{code}",
            "attachment_max_bytes": 1000,
            "attachment_timeout": 1.0,
            "attachment_store_chars": 1000,
        },
    )
    try:
        assert provider.board_type == "trello"
        assert provider.secrets == frozenset({API_KEY, TOKEN})
        assert provider._board_id() == BOARD
        assert str(provider._client.base_url) == f"{DEFAULT_API_BASE}/"
    finally:
        provider.close()


def test_non_https_api_base_is_rejected_before_any_request() -> None:
    with pytest.raises(BoardProviderError) as exc_info:
        TrelloBoard(api_key=API_KEY, api_token=TOKEN, api_base="http://api.trello.com/1")

    assert exc_info.value.category == "configuration"
    assert TOKEN not in repr(exc_info.value)


def test_validate_connection_warns_about_archived_board_and_prefix_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1/members/me":
            return httpx.Response(200, json={"id": "u-1"})
        if request.url.path == f"/1/boards/{BOARD}":
            return httpx.Response(200, json={"id": BOARD, "closed": True})
        return httpx.Response(200, json=[])

    result = _board(handler).validate_connection("OTHER")

    assert result["status"] == "ok"
    assert result["capabilities"] == {"read": True, "create": False, "done": False}
    assert any("закрыта" in warning for warning in result["warnings"])
    assert any("key_prefix" in warning for warning in result["warnings"])
