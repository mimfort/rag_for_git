"""Discovery целей Yandex Tracker: статусы организации + выбор очереди.

Роль цели играет **статус**, а закрытие выполняется переходом. Queue-специфичного
эндпоинта статусов в API нет, поэтому список берётся из
``GET /v3/statuses`` (https://yandex.ru/support/tracker/ru/concepts/get-statuses),
а очереди — из ``GET /v3/queues`` для подсказки option ``queue``.
"""
from __future__ import annotations

import httpx

from reviewer.tasks.boards.yandex_tracker import YandexTrackerBoard

TOKEN = "yandex-tracker-secret-token"

STATUSES = [
    {"id": 1, "key": "open", "name": "Открыт", "type": "new"},
    {"id": 2, "key": "inProgress", "name": "В работе", "type": "inProgress"},
    {"id": 3, "key": "closed", "name": "Закрыт", "type": "done"},
    {"id": 4, "key": "", "name": "", "type": "done"},
]
QUEUES = [
    {"id": 3, "key": "TREK", "name": "Trek"},
    {"id": 4, "key": "OTHER", "name": "Other"},
]


def _board(handler, **kwargs) -> YandexTrackerBoard:
    options: dict = {
        "token": TOKEN,
        "org_id": "org-42",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    options.update(kwargs)
    return YandexTrackerBoard(**options)


def _handler(*, statuses_status: int = 200, queues_status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/statuses":
            if statuses_status != 200:
                return httpx.Response(statuses_status, json={"token": TOKEN})
            return httpx.Response(200, json=STATUSES)
        if request.url.path == "/v3/queues":
            if queues_status != 200:
                return httpx.Response(queues_status, json={"token": TOKEN})
            return httpx.Response(200, json=QUEUES)
        return httpx.Response(404, json={})

    return handle


def test_list_targets_returns_the_normalized_shape() -> None:
    board = _board(_handler())

    result = board.list_targets("TREK")

    assert set(result) == {"targets", "options", "warnings"}
    assert result["targets"][0] == {"id": "open", "label": "Открыт", "purposes": ["create", "done"]}
    assert all("create" in target["purposes"] for target in result["targets"])
    assert [target["id"] for target in result["targets"]] == ["open", "inProgress", "closed"]
    assert result["warnings"] == []
    board.close()


def test_queue_option_choices_come_from_the_queues_endpoint() -> None:
    board = _board(_handler())

    result = board.list_targets("TREK")

    assert result["options"] == [
        {
            "key": "queue",
            "label": "Очередь (ключ, например TREK)",
            "required_for": ["sync", "create", "finish"],
            "choices": [
                {"id": "TREK", "label": "Trek"},
                {"id": "OTHER", "label": "Other"},
            ],
        }
    ]
    board.close()


def test_unavailable_queues_are_fail_soft_with_a_warning() -> None:
    board = _board(_handler(queues_status=403))

    result = board.list_targets("TREK")

    assert result["targets"]
    assert result["options"][0]["choices"] == []
    assert result["warnings"]
    assert TOKEN not in repr(result)
    board.close()


def test_missing_queue_is_reported_as_a_warning() -> None:
    board = _board(_handler())

    result = board.list_targets(None)

    assert result["targets"]
    assert any("очеред" in warning.lower() for warning in result["warnings"])
    board.close()


def test_statuses_failure_propagates_as_board_error() -> None:
    board = _board(_handler(statuses_status=403))

    try:
        board.list_targets("TREK")
    except Exception as error:  # noqa: BLE001 - проверяется категория безопасной ошибки
        assert getattr(error, "category", "") == "permission"
        assert TOKEN not in repr(error)
    else:  # pragma: no cover - discovery статусов обязателен
        raise AssertionError("ожидалась BoardProviderError")
    board.close()
