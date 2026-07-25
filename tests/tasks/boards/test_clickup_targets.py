"""Discovery целей ClickUp: статусы списка, форма ответа, validate_connection."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.clickup import API_BASE, ClickUpBoard, provider_spec
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import BoardProviderRegistry

TOKEN = "pk_clickup_targets_secret"
LIST_PAYLOAD = {
    "id": "901",
    "name": "Бэклог",
    "statuses": [
        {"status": "к выполнению", "orderindex": 0, "color": "#87909e", "type": "open"},
        {"status": "в работе", "orderindex": 1, "color": "#4194f6", "type": "custom"},
        {"status": "готово", "orderindex": 2, "color": "#6bc950", "type": "closed"},
    ],
}


def _board(handler, **kwargs) -> ClickUpBoard:
    params: dict = {
        "token": TOKEN,
        "list_id": "901",
        "key_prefix": "PRI",
        "key_pattern": r"PRI-\d+",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    params.update(kwargs)
    return ClickUpBoard(**params)


def _list_handler(payload: dict = LIST_PAYLOAD):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/list/901":
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/v2/user":
            return httpx.Response(200, json={"user": {"id": 7, "username": "robot"}})
        return httpx.Response(404, json={"err": "not found"})

    return handler


def test_list_targets_returns_the_normalized_discovery_shape() -> None:
    board = _board(_list_handler())
    result = board.list_targets("PRI")

    assert set(result) == {"targets", "options", "warnings"}
    assert result["warnings"] == []
    assert [target["id"] for target in result["targets"]] == [
        "к выполнению",
        "в работе",
        "готово",
    ]
    assert all(target["id"] == target["label"] for target in result["targets"])
    assert all("create" in target["purposes"] for target in result["targets"])
    assert all("done" in target["purposes"] for target in result["targets"])
    assert result["targets"][2]["type"] == "closed"
    board.close()


def test_list_targets_exposes_provider_options_with_required_for() -> None:
    board = _board(_list_handler())
    options = {option["key"]: option for option in board.list_targets("PRI")["options"]}

    assert set(options) == {"list_id", "key_prefix", "team_id"}
    assert options["list_id"]["required_for"] == ["sync", "create", "finish"]
    assert options["key_prefix"]["required_for"] == ["sync"]
    assert options["team_id"]["required_for"] == []
    board.close()


def test_numeric_project_is_used_as_the_list_id() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=LIST_PAYLOAD)

    board = _board(handler, list_id="")
    board.list_targets("777")

    assert seen == ["/api/v2/list/777"]
    board.close()


def test_missing_list_id_yields_no_targets_and_a_warning() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=LIST_PAYLOAD)

    board = _board(handler, list_id="")
    result = board.list_targets("PRI")

    assert result["targets"] == []
    assert requests == []
    assert any("list_id" in warning for warning in result["warnings"])
    board.close()


def test_list_without_statuses_warns_instead_of_failing() -> None:
    board = _board(_list_handler({"id": "901", "name": "Бэклог"}))
    result = board.list_targets("PRI")

    assert result["targets"] == []
    assert any("statuses" in warning for warning in result["warnings"])
    board.close()


def test_validate_connection_reports_identity_and_capabilities() -> None:
    board = _board(_list_handler())
    result = board.validate_connection("PRI")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["identity"] == {"id": 7, "username": "robot"}
    assert result["project"] == "PRI"
    assert result["capabilities"] == {"read": True, "create": True, "finish": True}
    assert result["warnings"] == []
    assert TOKEN not in repr(result)
    board.close()


def test_validate_connection_warns_when_the_list_is_not_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/user":
            return httpx.Response(200, json={"user": {"id": 7, "username": "robot"}})
        return httpx.Response(404, json={"err": "not found"})

    board = _board(handler, list_id="", key_prefix="")
    result = board.validate_connection(None)

    assert result["status"] == "ok"
    assert result["capabilities"] == {"read": True, "create": False, "finish": False}
    assert len(result["warnings"]) == 2
    board.close()


def test_validate_connection_tolerates_an_unwrapped_user_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/user":
            return httpx.Response(200, json={"id": 9, "username": "flat"})
        return httpx.Response(200, json=LIST_PAYLOAD)

    board = _board(handler)
    assert board.validate_connection("PRI")["identity"] == {"id": 9, "username": "flat"}
    board.close()


def test_provider_spec_metadata_is_complete() -> None:
    spec = provider_spec()
    credentials = {field.env: field for field in spec.credential_fields}

    assert spec.board_type == "clickup"
    assert spec.default_api_base == API_BASE
    assert credentials["CLICKUP_API_TOKEN"].secret is True
    assert credentials["CLICKUP_API_TOKEN"].required is True
    assert credentials["CLICKUP_API_BASE"].secret is False
    assert credentials["CLICKUP_API_BASE"].required is False
    assert credentials["CLICKUP_API_BASE"].default == API_BASE
    assert spec.setup.help_url.startswith("https://developer.clickup.com/")
    assert spec.create_target_label and spec.done_target_label


def test_provider_spec_passes_registry_validation_and_builds_a_provider() -> None:
    registry = BoardProviderRegistry([provider_spec()])

    provider = registry.create(
        "clickup",
        credentials={"CLICKUP_API_TOKEN": TOKEN, "CLICKUP_API_BASE": ""},
        options={"list_id": "901", "key_prefix": "PRI", "team_id": "9007"},
        build_defaults={
            "key_pattern": r"PRI-\d+",
            "url_template": "",
            "attachment_max_bytes": 1000,
            "attachment_timeout": 1.0,
            "attachment_store_chars": 1000,
        },
    )

    assert provider.board_type == "clickup"
    assert TOKEN not in repr(provider)
    provider.close()


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "authentication"), (403, "permission"), (404, "not_found")],
)
def test_validate_connection_maps_http_status_to_error_category(
    status: int,
    category: str,
) -> None:
    board = _board(lambda _request: httpx.Response(status, json={"token": TOKEN}))
    with pytest.raises(BoardProviderError) as exc_info:
        board.validate_connection("PRI")

    assert exc_info.value.category == category
    assert TOKEN not in f"{exc_info.value!s}{exc_info.value!r}"
    board.close()
