from reviewer.tasks.boards.yougile import YougileBoard

PR = "https://github.com/o/r/pull/7"


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
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return self._get[path]

    def put(self, path, json=None):
        self.calls.append(("PUT", path, json))
        return _Resp(200, {})

    def close(self):
        pass


def _board(get_routes):
    b = YougileBoard.__new__(YougileBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes)
    return b


def test_yougile_finish_marks_done_and_adds_pr():
    b = _board({"/tasks/PRI-10": _Resp(200, {"id": "u1", "description": "тело",
                                             "completed": False})})
    res = b.finish("PRI-10", PR)
    assert res["done_set"] is True
    assert res["pr_link_added"] is True
    assert res["already_closed"] is False
    put = next(c for c in b._client.calls if c[0] == "PUT")
    assert put[1] == "/tasks/u1"
    assert put[2]["completed"] is True
    assert PR in put[2]["description"]
    assert "тело" in put[2]["description"]


def test_yougile_finish_idempotent_when_pr_present_and_done():
    desc = f'тело<div>PR: <a href="{PR}">{PR}</a></div>'
    b = _board({"/tasks/PRI-10": _Resp(200, {"id": "u1", "description": desc,
                                             "completed": True})})
    res = b.finish("PRI-10", PR)
    assert res["already_closed"] is True
    assert res["pr_link_added"] is False
    assert res["done_set"] is False
    assert not [c for c in b._client.calls if c[0] == "PUT"]  # записи нет


def test_yougile_finish_note_appended():
    b = _board({"/tasks/PRI-10": _Resp(200, {"id": "u1", "description": "",
                                             "completed": False})})
    b.finish("PRI-10", PR, note="закрыто автоматически")
    put = next(c for c in b._client.calls if c[0] == "PUT")
    assert "закрыто автоматически" in put[2]["description"]
