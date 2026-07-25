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
        self.status_code = 200
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
    b._att_domains = ("yougile.com",)
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
            {"text": "", "properties": {
                "textHtml": "<p>/root/#file:https://yougile.com/f/d.txt</p>"}}]}),
        "https://yougile.com/f/d.txt": _FakeYResp(content="D".encode()),
    }
    board = _board_with(routes)
    brief = board.normalize(_raw(board_id=UUID, subtask_ids=[]))
    assert any(a["name"] == "d.txt" and a["content_text"] == "D" for a in brief["attachments"])


def test_yougile_normalize_skips_offhost_chat_file():
    """Файл из чата с off-host URL не скачивается (защита токена/SSRF, PRI-196)."""
    routes = {f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": [
        {"text": "/root/#file:https://evil.example.com/x.md"}]})}
    board = _board_with(routes)
    brief = board.normalize(_raw(board_id=UUID, subtask_ids=[]))
    assert brief["attachments"] == []
    assert "https://evil.example.com/x.md" not in board._client.requested


def test_yougile_normalize_pulls_file_from_description_href():
    """Файл-вложение приходит HTML-ссылкой на /user-data/ в description (реальная модель PRI-196).

    Так YouGile рендерит прикреплённый к задаче файл — не маркером /root/#file: в чате
    (чат при этом пуст), а <a href="…/user-data/…"> в описании задачи.
    """
    href = ('<p><br><a target="_blank" rel="noopener noreferrer" '
            'href="https://yougile.com/user-data/abc/Te.md">Te.md</a></p>')
    routes = {
        f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": []}),
        "https://yougile.com/user-data/abc/Te.md": _FakeYResp(content="Тестовая".encode()),
    }
    board = _board_with(routes)
    brief = board.normalize(_raw(key="ID-10", board_id=UUID, description=href, subtask_ids=[]))
    att = next(a for a in brief["attachments"] if a["name"] == "Te.md")
    assert att["content_text"] == "Тестовая"


def test_yougile_normalize_unescapes_amp_in_href():
    """&amp; в href восстанавливается в & перед скачиванием (URL с query-подписью)."""
    href = '<a href="https://yougile.com/user-data/x/d.txt?a=1&amp;b=2">d.txt</a>'
    routes = {
        f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": []}),
        "https://yougile.com/user-data/x/d.txt?a=1&b=2": _FakeYResp(content="D".encode()),
    }
    board = _board_with(routes)
    brief = board.normalize(_raw(board_id=UUID, description=href, subtask_ids=[]))
    assert any(a["name"] == "d.txt" and a["content_text"] == "D" for a in brief["attachments"])


def test_yougile_normalize_ignores_nonfile_description_links():
    """Обычная ссылка в описании (не на /user-data/) не качается — только файлы-вложения."""
    desc = '<a href="https://yougile.com/team/T/#PRI-5">PRI-5</a>'
    routes = {f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": []})}
    board = _board_with(routes)
    brief = board.normalize(_raw(board_id=UUID, description=desc, subtask_ids=[]))
    assert brief["attachments"] == []
    assert "https://yougile.com/team/T/#PRI-5" not in board._client.requested


def test_yougile_normalize_skips_offhost_description_href():
    """Off-host файл-ссылка в описании не скачивается (защита токена/SSRF, PRI-196)."""
    desc = '<a href="https://evil.example.com/user-data/x/leak.md">leak.md</a>'
    routes = {f"/chats/{UUID}/messages": _FakeYResp(json_data={"content": []})}
    board = _board_with(routes)
    brief = board.normalize(_raw(board_id=UUID, description=desc, subtask_ids=[]))
    assert brief["attachments"] == []
    assert "https://evil.example.com/user-data/x/leak.md" not in board._client.requested


def test_normalize_completed_maps_to_done_status():
    b = normalize_yougile(_raw(status="In progress", completed=True), KP, URL)
    assert b["status"] == "done"


def test_normalize_not_completed_keeps_column_status():
    b = normalize_yougile(_raw(status="In progress", completed=False), KP, URL)
    assert b["status"] == "In progress"


class _BoomClient:
    """httpx-заглушка: любой сетевой вызов = ошибка (доказывает отсутствие I/O)."""

    def get(self, *a, **k):
        raise AssertionError("normalize_meta не должен делать сетевые вызовы")

    def close(self):
        pass


def test_normalize_meta_no_io_yougile():
    b = YougileBoard(api_key="k", api_base="https://yougile.com/api-v2",
                     key_pattern=KP, url_template=URL)
    b._client = _BoomClient()
    # подзадачи и related-ссылки есть — дорогой normalize полез бы в сеть, meta — нет
    raw = _raw(subtask_ids=["u1"], description="связано с PRI-96")
    meta = b.normalize_meta(raw)
    assert meta["key"] == "ID-10"
    assert meta["aliases"] == ["PRI-10"]
    assert meta["status"] == "Backlog"
    assert meta["url"] == "https://ru.yougile.com/team/T/#PRI-10"
    assert meta["project"] == "PRI"
    assert meta["criteria"] == [] and meta["attachments"] == []


def test_normalize_converts_html_description_to_markdown():
    # YouGile хранит описание в HTML; в стор reviewer оно обязано попадать markdown-ом
    raw = RawTask(key="ID-1", project_code="PRI-1", title="t",
                  description='тело<div>PR: <a href="https://g/p/1">https://g/p/1</a></div>',
                  status="Бэклог", subtask_ids=[], timestamp=1)
    out = normalize_yougile(raw, r"PRI-\d+", "https://b/#{code}")
    assert "<div>" not in out["description"]
    assert "PR: https://g/p/1" in out["description"]


def test_normalize_keeps_plain_markdown_intact():
    raw = RawTask(key="ID-2", project_code="PRI-2", title="t",
                  description="## Проблема\n\nтекст", status=None,
                  subtask_ids=[], timestamp=1)
    out = normalize_yougile(raw, r"PRI-\d+", "https://b/#{code}")
    assert out["description"] == "## Проблема\n\nтекст"
