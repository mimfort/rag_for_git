from reviewer.tasks.boards.youtrack import YouTrackBoard


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    """Фейк httpx: роутит GET по path (admin id уже в пути). .json() отдаёт список."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        r = self.routes.get(path)
        if r is None:
            return _Resp(500, None)
        return r

    def close(self):
        pass


def _board(routes):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(routes)
    b._status_field = "State"
    return b


def test_youtrack_targets_admin_success():
    routes = {
        "/admin/projects": _Resp(200, [{"id": "0-5", "shortName": "TES"}]),
        "/admin/projects/0-5/customFields": _Resp(200, [
            {"$type": "StateProjectCustomField", "field": {"name": "Stage"},
             "bundle": {"values": [{"name": "Open"}, {"name": "Готово"}]}},
            {"$type": "TextProjectCustomField", "field": {"name": "Descr"},
             "bundle": None},  # не bundle-поле → пропуск
        ]),
    }
    board = _board(routes)
    board._status_field = "Stage"
    res = board.list_targets("TES")
    assert res["targets"] == [
        {"id": "Open", "label": "Open", "purposes": ["create", "done"]},
        {"id": "Готово", "label": "Готово", "purposes": ["create", "done"]},
    ]
    assert res["options"][0]["choices"] == [{"id": "Stage", "label": "Stage"}]
    assert res["warnings"] == []


def test_youtrack_targets_fallback_to_sample_on_admin_403():
    routes = {
        "/admin/projects": _Resp(403, None),  # нет admin-прав
        "/issues": _Resp(200, [
            {"customFields": [{"name": "Stage", "value": {"name": "Open"},
                               "$type": "StateIssueCustomField"}]},
            {"customFields": [{"name": "Stage", "value": {"name": "Готово"},
                               "$type": "StateIssueCustomField"}]},
        ]),
    }
    board = _board(routes)
    board._status_field = "Stage"
    res = board.list_targets("TES")
    assert [target["id"] for target in res["targets"]] == ["Open", "Готово"]
    assert res["warnings"]  # предупреждение про недоступный admin


def test_youtrack_targets_sample_ignores_non_dict_values():
    routes = {
        "/admin/projects": _Resp(403, None),
        "/issues": _Resp(200, [
            {"customFields": [{"name": "Stage", "value": {"name": "Open"}},
                              {"name": "Assignee", "value": "текст"},   # не dict → пропуск
                              {"name": "Sprints", "value": [{"name": "S1"}]}]},  # list → пропуск
        ]),
    }
    board = _board(routes)
    board._status_field = "Stage"
    res = board.list_targets("TES")
    assert [choice["id"] for choice in res["options"][0]["choices"]] == ["Stage"]


def test_youtrack_targets_total_failure_empty_failsoft():
    routes = {"/admin/projects": _Resp(403, None), "/issues": _Resp(500, None)}
    res = _board(routes).list_targets("TES")
    assert res["targets"] == []
    assert len(res["warnings"]) >= 1


def test_youtrack_targets_empty_project_uses_sample_no_admin_call():
    routes = {"/issues": _Resp(200, [
        {"customFields": [{"name": "State", "value": {"name": "Open"},
                           "$type": "StateIssueCustomField"}]}])}
    b = _board(routes)
    res = b.list_targets(None)
    assert [target["id"] for target in res["targets"]] == ["Open"]
    assert not any(path.startswith("/admin") for path, _ in b._client.calls)
