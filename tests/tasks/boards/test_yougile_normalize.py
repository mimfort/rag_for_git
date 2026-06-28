from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.yougile import YougileBoard, normalize_yougile

KP = r"[A-Z]+-\d+"
URL = "https://ru.yougile.com/team/T/#{code}"


def _raw(**kw):
    base = dict(key="ID-10", project_code="PRI-10", title="T", description="",
                status="Backlog", subtask_ids=[], timestamp=1)
    base.update(kw)
    return RawTask(**base)


def test_key_aliases_status_url():
    b = normalize_yougile(_raw(), KP, URL)
    assert b["key"] == "ID-10"
    assert b["aliases"] == ["PRI-10"]
    assert b["status"] == "Backlog"
    assert b["criteria"] == []
    assert b["url"] == "https://ru.yougile.com/team/T/#PRI-10"


def test_url_none_without_template():
    b = normalize_yougile(_raw(), KP, "")
    assert b["url"] is None


def test_related_links_from_description_excluding_self():
    raw = _raw(description="связано с PRI-96 и ID-10 и PRI-10 и ABC-7")
    b = normalize_yougile(raw, KP, URL)
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert rels == {"PRI-96", "ABC-7"}        # self key ID-10 и alias PRI-10 исключены


def test_subtask_links_with_titles_and_related_dedup():
    raw = _raw(description="см. ID-55", subtask_ids=["u1"])
    b = normalize_yougile(raw, KP, URL, subtask_titles={"u1": "ID-55:Подзадача"})
    sub = [lk for lk in b["links"] if lk["type"] == "subtask"]
    assert sub == [{"type": "subtask", "key": "u1", "title": "ID-55:Подзадача"}]
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert "ID-55" in rels                     # код ID-55 — отдельный от UUID u1


def test_alias_omitted_when_equals_key():
    b = normalize_yougile(_raw(project_code="ID-10"), KP, URL)
    assert b["aliases"] == []


def test_subtask_without_title_keeps_edge():
    raw = _raw(subtask_ids=["u9"])
    b = normalize_yougile(raw, KP, URL)
    assert b["links"] == [{"type": "subtask", "key": "u9"}]


def test_normalize_sets_project_prefix():
    b = normalize_yougile(_raw(project_code="PRI-10"), KP, URL)
    assert b["project"] == "PRI"


def test_normalize_includes_injected_attachments():
    atts = [{"name": "tz.docx", "mime_type": None, "size": 9, "content_text": "тз"}]
    b = normalize_yougile(_raw(), KP, URL, attachments=atts)
    assert b["attachments"] == atts


def test_normalize_attachments_default_empty():
    assert normalize_yougile(_raw(), KP, URL)["attachments"] == []


UUID = "09e30301-4d72-46c8-9161-87cb1ab32487"


class _FakeYResp:
    def __init__(self, json_data=None, content=b"", headers=None):
        self._json = json_data or {}
        self.content = content
        self.headers = headers or {"Content-Length": str(len(content))}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeYClient:
    """Маршрутизирует GET по точному пути/URL; отсутствие роута → KeyError (fail-soft внутри)."""
    def __init__(self, routes):
        self._routes = routes
        self.requested = []

    def get(self, path, params=None, timeout=None):
        self.requested.append(path)
        if path not in self._routes:
            raise KeyError(path)
        return self._routes[path]

    def close(self):
        pass


def _board_with(routes):
    b = YougileBoard.__new__(YougileBoard)   # обойти httpx.Client в __init__
    b._key_pattern = KP
    b._url_template = URL
    b._att_max_bytes = 10 * 1024 * 1024
    b._att_timeout = 10.0
    b._att_store_chars = 200000
    b._client = _FakeYClient(routes)
    return b


def test_yougile_normalize_pulls_chat_file():
    routes = {
        f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": [
            {"text": "вот ТЗ\n/root/#file:https://yougile.com/files/abc/tz.md"}]}),
        "https://yougile.com/files/abc/tz.md": _FakeYResp(content="тело ТЗ".encode()),
    }
    board = _board_with(routes)
    raw = _raw(key="ID-10", board_id=UUID, subtask_ids=[])
    brief = board.normalize(raw)
    names = [a["name"] for a in brief["attachments"]]
    assert "tz.md" in names
    att = next(a for a in brief["attachments"] if a["name"] == "tz.md")
    assert att["content_text"] == "тело ТЗ"
    assert f"/chats/{UUID}/messages" in board._client.requested


def test_yougile_normalize_dedups_repeated_file_url():
    routes = {
        f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": [
            {"text": "/root/#file:https://yougile.com/f/x/a.md"},
            {"text": "повтор /root/#file:https://yougile.com/f/x/a.md"}]}),
        "https://yougile.com/f/x/a.md": _FakeYResp(content="A".encode()),
    }
    board = _board_with(routes)
    brief = board.normalize(_raw(board_id=UUID, subtask_ids=[]))
    assert len([a for a in brief["attachments"] if a["name"] == "a.md"]) == 1


def test_yougile_normalize_ignores_plain_messages():
    routes = {f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": [
        {"text": "обычное сообщение без файла"}]})}
    board = _board_with(routes)
    assert board.normalize(_raw(board_id=UUID, subtask_ids=[]))["attachments"] == []


def test_yougile_normalize_failsoft_when_chat_unavailable():
    board = _board_with({})   # нет роута чата → KeyError → fail-soft, normalize не падает
    brief = board.normalize(_raw(board_id=UUID, subtask_ids=[]))
    assert brief["attachments"] == []


def test_yougile_normalize_marker_in_texthtml():
    routes = {
        f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": [
            {"text": "", "properties": {"textHtml": "<p>/root/#file:https://yg/f/d.txt</p>"}}]}),
        "https://yg/f/d.txt": _FakeYResp(content="D".encode()),
    }
    board = _board_with(routes)
    brief = board.normalize(_raw(board_id=UUID, subtask_ids=[]))
    assert any(a["name"] == "d.txt" and a["content_text"] == "D" for a in brief["attachments"])
