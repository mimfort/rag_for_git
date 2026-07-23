"""Создание задачи в YouGile (PRI-213)."""
import pytest

from reviewer.tasks.boards.yougile import YougileBoard

MD = "## Проблема\n\nтекст"


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
    def __init__(self, get_routes, post_resp=None):
        self._get = get_routes
        self._post = post_resp or _Resp(200, {"id": "u-new"})
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path not in self._get:
            raise RuntimeError(f"нет маршрута {path}")
        return self._get[path]

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return self._post

    def close(self):
        pass


def _board(get_routes, post_resp=None):
    b = YougileBoard.__new__(YougileBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_resp)
    b._key_pattern = r"PRI-\d+"
    b._url_template = "https://b/#{code}"
    return b


def _routes(columns):
    return {
        "/projects": _Resp(200, {"content": [{"id": "p1"}]}),
        "/boards": _Resp(200, {"content": [{"id": "b1", "title": "доска"}]}),
        "/columns": _Resp(200, {"content": columns}),
        "/tasks": _Resp(200, {"content": [{"idTaskProject": "PRI-1"}]}),
        "/tasks/u-new": _Resp(200, {"idTaskProject": "PRI-42", "id": "u-new"}),
    }


def test_create_puts_task_into_requested_column():
    b = _board(_routes([{"id": "c1", "title": "Бэклог"},
                        {"id": "c2", "title": "Движок"}]))
    res = b.create(MD, title="Заголовок", target="Движок", project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST")
    assert post[1] == "/tasks"
    assert post[2]["columnId"] == "c2"
    assert post[2]["title"] == "Заголовок"
    assert res["target_resolved"] == "Движок"
    assert not res["warnings"]


def test_create_sends_html_description():
    b = _board(_routes([{"id": "c1", "title": "Бэклог"}]))
    b.create(MD, title="t", target=None, project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST")
    assert "<h2>Проблема</h2>" in post[2]["description"]


def test_create_resolves_project_key_with_second_get():
    # POST /tasks отдаёт только uuid; проектный код (PRI-N) присваивает доска
    b = _board(_routes([{"id": "c1", "title": "Бэклог"}]))
    res = b.create(MD, title="t", target=None, project="PRI")
    assert res["key"] == "PRI-42"
    assert res["board_id"] == "u-new"
    assert res["url"] == "https://b/#PRI-42"


def test_create_falls_back_to_first_column_with_warning():
    b = _board(_routes([{"id": "c1", "title": "Бэклог"}]))
    res = b.create(MD, title="t", target="Нет такой", project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST")
    assert post[2]["columnId"] == "c1"
    assert res["target_resolved"] == "Бэклог"
    assert res["warnings"]


def test_create_failsoft_when_key_lookup_fails():
    routes = _routes([{"id": "c1", "title": "Бэклог"}])
    routes.pop("/tasks/u-new")            # второй GET недоступен
    b = _board(routes)
    res = b.create(MD, title="t", target=None, project="PRI")
    assert res["key"] == "u-new"          # деградация до внутреннего id
    assert res["warnings"]


def test_create_warns_when_second_get_lacks_project_key():
    # второй GET отвечает 200, но БЕЗ idTaskProject (например, задача ещё не привязана
    # к проекту на стороне YouGile) — деградация до uuid должна сопровождаться warning'ом
    routes = _routes([{"id": "c1", "title": "Бэклог"}])
    routes["/tasks/u-new"] = _Resp(200, {"id": "u-new"})  # нет idTaskProject
    b = _board(routes)
    res = b.create(MD, title="t", target=None, project="PRI")
    assert res["key"] == "u-new"          # деградация до внутреннего id — данные не теряются
    assert res["board_id"] == "u-new"
    assert res["warnings"]
    assert any("idTaskProject" in w for w in res["warnings"])


def test_create_raises_when_no_columns():
    b = _board({"/projects": _Resp(200, {"content": []})})
    with pytest.raises(RuntimeError):
        b.create(MD, title="t", target=None, project="PRI")
