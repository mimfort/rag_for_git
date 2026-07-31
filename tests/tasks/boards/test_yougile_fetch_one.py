"""fetch_one(key) — единичный RawTask по ключу для write-through после finish."""
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
        return self._get[path]

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
        "/columns/c1": _Resp(200, {"title": "Готово"}),
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


def test_fetch_one_none_on_http_error():
    # 404/сеть → None (write-through пропускается, не валит finish).
    b = _board({"/tasks/PRI-404": _Resp(404, {})})
    assert b.fetch_one("PRI-404") is None


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
