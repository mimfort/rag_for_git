"""Создание задачи в YouTrack (PRI-213)."""
import pytest

from reviewer.tasks.boards.youtrack import YouTrackBoard

MD = "## Проблема\n\nтекст"


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes, post_routes=None):
        self._get = get_routes
        self._post = post_routes or {}
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path not in self._get:
            raise RuntimeError(f"нет маршрута {path}")
        return self._get[path]

    def post(self, path, json=None, params=None):
        self.calls.append(("POST", path, json))
        return self._post.get(path, _Resp(200, {"idReadable": "PRI-42"}))

    def close(self):
        pass


def _board(get_routes, post_routes=None, status_field="State"):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_routes)
    b._key_pattern = r"PRI-\d+"
    b._base = "https://yt.example/api"
    b._status_field = status_field
    return b


def _routes():
    return {
        "/admin/projects": _Resp(200, [{"id": "0-1", "shortName": "PRI"}]),
        "/issues/PRI-42": _Resp(200, {"customFields": [
            {"name": "State", "$type": "StateIssueCustomField",
             "value": {"$type": "StateBundleElement", "name": "Open"}}]}),
    }


def test_create_posts_markdown_as_is():
    b = _board(_routes())
    res = b.create(MD, title="Заголовок", target=None, project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST" and c[1] == "/issues")
    assert post[2]["description"] == MD          # YouTrack хранит markdown нативно
    assert post[2]["summary"] == "Заголовок"
    assert post[2]["project"] == {"id": "0-1"}
    assert res["key"] == "PRI-42"
    assert res["url"] == "https://yt.example/issue/PRI-42"


def test_create_sets_status_field_when_target_given():
    b = _board(_routes())
    res = b.create(MD, title="t", target="In Progress", project="PRI")
    upd = next(c for c in b._client.calls
               if c[0] == "POST" and c[1] == "/issues/PRI-42")
    field = upd[2]["customFields"][0]
    assert field["name"] == "State"
    assert field["value"]["name"] == "In Progress"
    assert res["target_resolved"] == "In Progress"
    assert not res["warnings"]


def test_create_failsoft_when_status_update_rejected():
    b = _board(_routes(), post_routes={"/issues/PRI-42": _Resp(400, {})})
    res = b.create(MD, title="t", target="Нет такого", project="PRI")
    assert res["key"] == "PRI-42"        # задача создана
    assert res["target_resolved"] is None
    assert res["warnings"]


def test_create_requires_project():
    b = _board(_routes())
    with pytest.raises(ValueError):
        b.create(MD, title="t", target=None, project=None)


def test_create_raises_when_project_unknown():
    b = _board({"/admin/projects": _Resp(200, [])})
    with pytest.raises(ValueError):
        b.create(MD, title="t", target=None, project="NOPE")


def test_create_warns_when_idreadable_missing_no_target():
    # POST /issues вернул 200, но без idReadable — задача создана «вслепую»:
    # это должно попасть в warnings, а не тихо проглатываться.
    b = _board(_routes(), post_routes={"/issues": _Resp(200, {})})
    res = b.create(MD, title="t", target=None, project="PRI")
    assert res["key"] == ""
    assert any("idReadable" in w for w in res["warnings"])


def test_create_warns_target_not_applied_when_idreadable_missing():
    # Тот же случай, но с запрошенным target: молчаливая деградация — target
    # тихо не применяется (нет ключа, на котором его выставлять). Должен быть
    # отдельный warning, явно называющий непринятый target.
    b = _board(_routes(), post_routes={"/issues": _Resp(200, {})})
    res = b.create(MD, title="t", target="In Progress", project="PRI")
    assert res["key"] == ""
    assert res["target_resolved"] is None
    assert any("idReadable" in w for w in res["warnings"])
    assert any("In Progress" in w and "target" in w.lower() for w in res["warnings"])
    # без ключа выставлять поле не на чем — запроса на смену статуса быть не должно
    assert not any(c[0] == "POST" and c[1].startswith("/issues/") for c in b._client.calls)
