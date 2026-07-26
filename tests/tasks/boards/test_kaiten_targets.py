"""Discovery целей Kaiten: колонки доски, машинный ``type`` → purposes, резолв id/label."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from tests.tasks.boards.test_kaiten_read import TOKEN, board, columns


def columns_handler(payload: list[dict] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/latest/boards/1/columns":
            return httpx.Response(200, json=payload if payload is not None else columns())
        return httpx.Response(404, json={})

    return handle


def test_list_targets_returns_the_normalized_shape() -> None:
    result = board(columns_handler()).list_targets("KTN")

    assert set(result) == {"targets", "options", "warnings"}
    assert result["warnings"] == []
    assert [target["id"] for target in result["targets"]] == ["1", "2", "4", "3"]
    assert all("create" in target["purposes"] for target in result["targets"])
    assert {"id", "label", "purposes"} <= set(result["targets"][0])


def test_done_purpose_comes_from_the_numeric_column_type() -> None:
    targets = {t["id"]: t for t in board(columns_handler()).list_targets("KTN")["targets"]}

    assert targets["3"] == {"id": "3", "label": "Готово", "purposes": ["create", "done"],
                            "kind": "done"}
    assert targets["1"]["purposes"] == ["create"]
    assert targets["2"]["kind"] == "in_progress"
    assert targets["4"]["label"] == "Ревью"


def test_missing_board_id_yields_a_warning_instead_of_an_error() -> None:
    result = board(columns_handler(), board_id="").list_targets("KTN")

    assert result["targets"] == []
    assert result["warnings"]


def test_board_without_a_done_column_is_reported() -> None:
    payload = [{"id": 1, "title": "Очередь", "type": 1, "sort_order": 1, "subcolumns": []}]

    result = board(columns_handler(payload)).list_targets("KTN")

    assert [target["id"] for target in result["targets"]] == ["1"]
    assert any("done" in warning for warning in result["warnings"])


def test_unavailable_columns_degrade_to_a_warning() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"token": TOKEN})

    result = board(handle).list_targets("KTN")

    assert result["targets"] == []
    assert result["warnings"]
    assert TOKEN not in repr(result)


@pytest.mark.parametrize("target", ["3", "Готово"])
def test_target_resolves_by_exact_id_and_by_exact_label(target: str) -> None:
    provider = board(columns_handler())

    column, warnings = provider._resolve_target(provider._columns("1"), target)

    assert column is not None
    assert column["id"] == 3
    assert warnings == []


def test_unknown_target_reports_a_warning_without_raising() -> None:
    provider = board(columns_handler())

    column, warnings = provider._resolve_target(provider._columns("1"), "Missing")

    assert column is None
    assert warnings and "Missing" in warnings[0]


def test_ambiguous_target_label_is_not_guessed() -> None:
    payload = [
        {"id": 1, "title": "Готово", "type": 3, "sort_order": 1, "subcolumns": []},
        {"id": 2, "title": "Готово", "type": 3, "sort_order": 2, "subcolumns": []},
    ]
    provider = board(columns_handler(payload))

    column, warnings = provider._resolve_target(provider._columns("1"), "Готово")

    assert column is None
    assert warnings and "неоднозначна" in warnings[0]


def test_validate_connection_reports_identity_and_capabilities() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/latest/users/current":
            return httpx.Response(200, json={"id": 9, "full_name": "Робот",
                                             "username": "robot", "company_id": 3})
        return columns_handler()(request)

    result = board(handle).validate_connection("KTN")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["identity"] == {"id": 9, "name": "Робот", "username": "robot"}
    assert result["project"] == "KTN"
    assert result["capabilities"] == {"read": True, "create": True, "finish": True,
                                      "attachments": True}
    assert result["warnings"] == []
    assert TOKEN not in repr(result)


def test_validate_connection_without_board_id_warns_about_write_capabilities() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/latest/users/current":
            return httpx.Response(200, json={"id": 9, "full_name": "Робот"})
        return httpx.Response(404, json={})

    result = board(handle, board_id="").validate_connection(None)

    assert result["status"] == "ok"
    assert result["capabilities"]["create"] is False
    assert result["capabilities"]["finish"] is False
    assert result["warnings"]


@pytest.mark.parametrize(("status", "category"), [(403, "permission"), (404, "not_found")])
def test_validate_connection_maps_transport_errors(status: int, category: str) -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"token": TOKEN})

    with pytest.raises(BoardProviderError) as error:
        board(handle).validate_connection("KTN")

    assert error.value.category == category
    assert TOKEN not in f"{error.value!s}{error.value!r}"


def test_validate_connection_surfaces_board_permission_errors() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/latest/users/current":
            return httpx.Response(200, json={"id": 9, "full_name": "Робот"})
        return httpx.Response(403, json={"token": TOKEN})

    with pytest.raises(BoardProviderError) as error:
        board(handle).validate_connection("KTN")

    assert error.value.category == "permission"
