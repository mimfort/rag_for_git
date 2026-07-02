from reviewer.tasks.boards.yougile import YougileBoard


class _Resp:
    def __init__(self, status=200, content=None):
        self.status_code = status
        self._content = content if content is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"content": self._content}


class _Client:
    """Фейк httpx: роутит GET по (path + дискриминирующий param). Неизвестный путь → 500
    (для проверки fail-soft). _get_all читает .json()['content']."""

    def __init__(self, routes):
        self.routes = routes  # ключ "path" или "path?disc=val" -> list[dict]
        self.calls = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        for disc in ("projectId", "boardId", "columnId"):
            if disc in params:
                key = f"{path}?{disc}={params[disc]}"
                return _Resp(200, self.routes[key]) if key in self.routes else _Resp(500)
        return _Resp(200, self.routes[path]) if path in self.routes else _Resp(500)

    def close(self):
        pass


def _board(routes):
    b = YougileBoard.__new__(YougileBoard)  # обойти httpx.Client в __init__
    b._client = _Client(routes)
    return b


_TWO_BOARDS = {
    "/projects": [{"id": "p1", "title": "Proj"}],
    "/boards?projectId=p1": [{"id": "b1", "title": "Board One"},
                             {"id": "b2", "title": "Board Two"}],
    "/columns?boardId=b1": [{"id": "c1", "title": "В работе"},
                            {"id": "c2", "title": "Готово"}],
    "/columns?boardId=b2": [{"id": "c3", "title": "Todo"},
                            {"id": "c4", "title": "Done"}],
    "/tasks?columnId=c1": [{"idTaskProject": "PRI-1"}],  # b1 хостит PRI
    "/tasks?columnId=c2": [],
    "/tasks?columnId=c3": [{"idTaskProject": "TES-1"}],  # b2 хостит только TES
    "/tasks?columnId=c4": [],
}


def test_yougile_targets_scopes_to_project_boards():
    res = _board(_TWO_BOARDS).list_done_targets("PRI")
    titles = {(c["title"], c["board_title"]) for c in res["columns"]}
    assert titles == {("В работе", "Board One"), ("Готово", "Board One")}
    assert res["warnings"] == []
    assert all(c["board_id"] == "b1" for c in res["columns"])


def test_yougile_targets_empty_project_returns_all_boards():
    b = _board(_TWO_BOARDS)
    res = b.list_done_targets(None)
    assert {c["title"] for c in res["columns"]} == {"В работе", "Готово", "Todo", "Done"}
    # без project задачи не сканируются вовсе
    assert not any(path == "/tasks" for path, _ in b._client.calls)


def test_yougile_targets_no_project_boards_warns():
    res = _board(_TWO_BOARDS).list_done_targets("ZZZ")
    assert res["columns"] == []
    assert res["warnings"]  # «колонки для проекта 'ZZZ' не найдены»


def test_yougile_targets_failsoft_on_error():
    # отсутствует роут /boards?projectId=p1 → 500 внутри обхода → warning, без падения
    routes = {"/projects": [{"id": "p1", "title": "Proj"}]}
    res = _board(routes).list_done_targets("PRI")
    assert res["columns"] == []
    assert res["warnings"]
