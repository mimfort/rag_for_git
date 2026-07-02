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
    res = _board(routes).list_done_targets("TES")
    assert res["source"] == "admin"
    assert res["status_fields"] == [
        {"field": "Stage", "values": ["Open", "Готово"], "$type": "StateProjectCustomField"}]
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
    res = _board(routes).list_done_targets("TES")
    assert res["source"] == "sample"
    assert res["status_fields"] == [
        {"field": "Stage", "values": ["Open", "Готово"], "$type": "StateIssueCustomField"}]
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
    res = _board(routes).list_done_targets("TES")
    assert [f["field"] for f in res["status_fields"]] == ["Stage"]


def test_youtrack_targets_total_failure_empty_failsoft():
    routes = {"/admin/projects": _Resp(403, None), "/issues": _Resp(500, None)}
    res = _board(routes).list_done_targets("TES")
    assert res["status_fields"] == []
    assert res["source"] == "sample"
    assert len(res["warnings"]) >= 1


def test_youtrack_targets_empty_project_uses_sample_no_admin_call():
    routes = {"/issues": _Resp(200, [
        {"customFields": [{"name": "State", "value": {"name": "Open"},
                           "$type": "StateIssueCustomField"}]}])}
    b = _board(routes)
    res = b.list_done_targets(None)
    assert res["source"] == "sample"
    assert [f["field"] for f in res["status_fields"]] == ["State"]
    assert not any(path.startswith("/admin") for path, _ in b._client.calls)
