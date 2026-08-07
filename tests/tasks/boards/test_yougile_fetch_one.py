"""fetch_one(key) — единичный RawTask по ключу для write-through после finish."""
import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.yougile import YougileBoard


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes):
        self._get = get_routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        result = self._get[path]
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        pass


def _board(get_routes):
    b = YougileBoard.__new__(YougileBoard)
    b._client = _Client(get_routes)
    return b


def test_fetch_one_builds_rawtask_like_iter_raw():
    b = _board({
        "/tasks/PRI-10": _Resp(200, {
            "id": "u1", "idTaskCommon": "ID-10", "idTaskProject": "PRI-10",
            "title": "Заголовок", "description": "тело", "columnId": "c1",
            "subtasks": ["s1", "s2"], "timestamp": 123, "completed": True}),
        "/columns/c1": _Resp(200, {"title": "Готово", "boardId": "board-1"}),
    })
    raw = b.fetch_one("PRI-10")
    assert raw is not None
    assert raw.key == "ID-10"                 # idTaskCommon предпочтительнее id
    assert raw.project_code == "PRI-10"
    assert raw.title == "Заголовок"
    assert raw.description == "тело"
    assert raw.status == "Готово"             # резолв колонки
    assert raw.subtask_ids == ["s1", "s2"]
    assert raw.timestamp == 123
    assert raw.board_id == "u1"
    assert raw.terminal is True
    assert raw.archived is None
    assert raw.provider_data == {
        "source_board_id": "board-1",
        "source_column_id": "c1",
    }


def test_fetch_one_none_only_on_not_found():
    b = _board({"/tasks/PRI-404": _Resp(404, {})})
    assert b.fetch_one("PRI-404") is None


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (401, "authentication", False),
        (403, "permission", False),
        (429, "rate_limit", True),
        (503, "transient", True),
    ],
)
def test_fetch_one_propagates_typed_http_error(status, category, retryable):
    b = _board({"/tasks/PRI-10": _Resp(status, {})})

    with pytest.raises(BoardProviderError) as raised:
        b.fetch_one("PRI-10")

    assert raised.value.category == category
    assert raised.value.retryable is retryable


def test_fetch_one_propagates_network_error_as_retryable_transient():
    request = httpx.Request("GET", "https://yougile.test/tasks/PRI-10")
    b = _board({"/tasks/PRI-10": httpx.ReadTimeout("timeout", request=request)})

    with pytest.raises(BoardProviderError) as raised:
        b.fetch_one("PRI-10")

    assert raised.value.category == "transient"
    assert raised.value.retryable is True


def test_fetch_one_propagates_invalid_json_error():
    class InvalidJsonResponse(_Resp):
        def json(self):
            raise ValueError("invalid json")

    b = _board({"/tasks/PRI-10": InvalidJsonResponse(200)})

    with pytest.raises(BoardProviderError) as raised:
        b.fetch_one("PRI-10")

    assert raised.value.category == "unsupported"
    assert raised.value.retryable is False


def test_fetch_one_survives_column_resolve_failure():
    # колонка не резолвится → status=None, но RawTask собран (terminal даст done).
    b = _board({
        "/tasks/PRI-10": _Resp(200, {
            "id": "u1", "idTaskProject": "PRI-10", "title": "T",
            "description": "d", "columnId": "c1", "completed": True}),
        "/columns/c1": _Resp(500, {}),
    })
    raw = b.fetch_one("PRI-10")
    assert raw is not None
    assert raw.status is None
    assert raw.timestamp is None
    assert raw.terminal is True
    assert raw.archived is None


def test_fetch_one_ignores_nonboolean_completed_value():
    b = _board({
        "/tasks/PRI-10": _Resp(200, {
            "id": "u1", "idTaskProject": "PRI-10", "completed": "true",
            "timestamp": "invalid"}),
    })

    raw = b.fetch_one("PRI-10")

    assert raw is not None
    assert raw.timestamp is None
    assert raw.terminal is None
    assert raw.archived is None
