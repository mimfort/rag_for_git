"""Discovery целей Weeek: колонки доски в общей форме targets + резолв по id/label."""
from __future__ import annotations

from typing import Any

import httpx

from reviewer.tasks.boards.weeek import WeeekBoard

TOKEN = "weeek-secret-token"
COLUMNS = {
    "success": True,
    "boardColumns": [
        {"id": 8, "name": "Backlog", "boardId": 6},
        {"id": 9, "name": "Done", "boardId": 6},
        {"id": 10, "name": "Done", "boardId": 6},
    ],
}


def _board(handler, **kwargs) -> WeeekBoard:
    params: dict[str, Any] = {
        "api_token": TOKEN,
        "project_id": "4",
        "board_id": "6",
        "key_prefix": "WEEEK",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _seconds: None,
    }
    params.update(kwargs)
    return WeeekBoard(**params)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/tm/board-columns"):
        return httpx.Response(200, json=COLUMNS)
    return httpx.Response(404, json={"success": False})


def test_list_targets_returns_board_columns_in_the_normalized_shape() -> None:
    result = _board(_handler).list_targets("WEEEK")

    assert set(result) == {"targets", "options", "warnings"}
    assert result["warnings"] == []
    assert [(target["id"], target["label"]) for target in result["targets"]] == [
        ("8", "Backlog"),
        ("9", "Done"),
        ("10", "Done"),
    ]
    assert all("create" in target["purposes"] for target in result["targets"])
    assert all("done" in target["purposes"] for target in result["targets"])
    assert {"id", "label", "purposes"} <= set(result["targets"][0])


def test_options_expose_project_board_and_key_prefix() -> None:
    options = _board(_handler).list_targets(None)["options"]

    assert [option["key"] for option in options] == ["project_id", "board_id", "key_prefix"]
    assert options[0]["required_for"] == ["sync", "create", "finish"]
    assert options[2]["required_for"] == ["sync"]


def test_columns_are_requested_once_for_the_configured_board() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return _handler(request)

    provider = _board(handler)
    provider.list_targets(None)
    provider.list_targets(None)

    assert seen == [{"boardId": "6"}]


def test_missing_board_is_a_warning_not_an_error() -> None:
    result = _board(_handler, board_id="").list_targets(None)

    assert result["targets"] == []
    assert result["warnings"]


def test_unavailable_columns_degrade_to_a_warning_without_leaking_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"token": TOKEN})

    result = _board(handler).list_targets(None)

    assert result["targets"] == []
    assert result["warnings"]
    assert TOKEN not in repr(result)


def test_target_resolves_by_column_id_and_by_unique_label() -> None:
    provider = _board(_handler)

    by_id, warnings_by_id = provider._resolve_column("8")
    by_label, warnings_by_label = provider._resolve_column("Backlog")

    assert by_id == {"id": 8, "label": "Backlog"}
    assert by_label == {"id": 8, "label": "Backlog"}
    assert warnings_by_id == []
    assert warnings_by_label == []


def test_ambiguous_label_is_not_applied_and_reports_a_warning() -> None:
    resolved, warnings = _board(_handler)._resolve_column("Done")

    assert resolved is None
    assert any("Done" in warning for warning in warnings)


def test_missing_target_reports_a_warning_and_no_column() -> None:
    resolved, warnings = _board(_handler)._resolve_column("Missing")

    assert resolved is None
    assert any("Missing" in warning for warning in warnings)
