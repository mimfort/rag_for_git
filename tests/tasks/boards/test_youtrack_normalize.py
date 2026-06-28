from reviewer.tasks.boards.youtrack import (
    YouTrackBoard,
    _attachments_of,
    _issue_to_raw,
    _origin,
    normalize_youtrack,
)

KP = r"[A-Z]+-\d+"
BASE = "https://c.youtrack.cloud/api"


def _issue(**kw):
    base = {
        "idReadable": "PRJ-7",
        "summary": "Заголовок",
        "description": "",
        "updated": 1700000000000,
        "customFields": [{"name": "State", "value": {"name": "In Progress"}}],
        "links": [],
    }
    base.update(kw)
    return base


def test_issue_to_raw_basic():
    raw = _issue_to_raw(_issue())
    assert raw.key == "PRJ-7"
    assert raw.project_code == "PRJ-7"
    assert raw.title == "Заголовок"
    assert raw.status == "In Progress"
    assert raw.timestamp == 1700000000000
    assert raw.subtask_ids == []


def test_issue_to_raw_state_missing():
    raw = _issue_to_raw(_issue(customFields=[{"name": "Priority",
                                              "value": {"name": "High"}}]))
    assert raw.status is None


def test_issue_to_raw_links_subtask_vs_related():
    issue = _issue(links=[
        {"direction": "OUTWARD", "linkType": {"name": "Subtask"},
         "issues": [{"idReadable": "PRJ-8"}]},
        {"direction": "OUTWARD", "linkType": {"name": "Relates"},
         "issues": [{"idReadable": "PRJ-9"}]},
    ])
    raw = _issue_to_raw(issue)
    assert {"type": "subtask", "key": "PRJ-8"} in raw.links
    assert {"type": "related", "key": "PRJ-9"} in raw.links


def test_normalize_url_and_aliases():
    raw = _issue_to_raw(_issue())
    b = normalize_youtrack(raw, KP, BASE)
    assert b["key"] == "PRJ-7"
    assert b["aliases"] == []
    assert b["criteria"] == []
    assert b["status"] == "In Progress"
    assert b["url"] == "https://c.youtrack.cloud/issue/PRJ-7"   # /api отброшен


def test_normalize_url_none_without_base():
    raw = _issue_to_raw(_issue())
    assert normalize_youtrack(raw, KP, "")["url"] is None


def test_normalize_related_from_description_excludes_self_and_links():
    issue = _issue(description="зависит от ABC-1 и PRJ-7 и PRJ-8",
                   links=[{"direction": "OUTWARD", "linkType": {"name": "Subtask"},
                           "issues": [{"idReadable": "PRJ-8"}]}])
    raw = _issue_to_raw(issue)
    b = normalize_youtrack(raw, KP, BASE)
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert rels == {"ABC-1"}            # self PRJ-7 и уже-связанный PRJ-8 исключены


def test_normalize_null_description():
    raw = _issue_to_raw(_issue(description=None))
    b = normalize_youtrack(raw, KP, BASE)
    assert b["description"] == ""
    assert b["links"] == []


def test_issue_to_raw_state_non_dict_value():
    raw = _issue_to_raw(_issue(customFields=[{"name": "State", "value": None}]))
    assert raw.status is None
    raw2 = _issue_to_raw(_issue(customFields=[{"name": "State", "value": "Done"}]))
    assert raw2.status is None


def test_normalize_sets_project_prefix():
    raw = _issue_to_raw(_issue(idReadable="PRJ-7"))
    b = normalize_youtrack(raw, KP, BASE)
    assert b["project"] == "PRJ"


def test_normalize_includes_injected_attachments():
    raw = _issue_to_raw(_issue())
    atts = [{"name": "spec.md", "mime_type": "text/markdown", "size": 4, "content_text": "spec"}]
    b = normalize_youtrack(raw, KP, BASE, attachments=atts)
    assert b["attachments"] == atts


def test_normalize_attachments_default_empty():
    raw = _issue_to_raw(_issue())
    assert normalize_youtrack(raw, KP, BASE)["attachments"] == []


def test_origin_strips_api_path():
    assert _origin("https://c.youtrack.cloud/api") == "https://c.youtrack.cloud"


def test_attachments_of_extracts_metadata():
    issue = _issue(attachments=[
        {"name": "spec.md", "mimeType": "text/markdown", "size": 4,
         "url": "/api/files/7-2?sign=abc"},
        {"name": "nourl", "mimeType": "text/plain"},   # без url — пропускается
    ])
    atts = _attachments_of(issue)
    assert atts == [{"name": "spec.md", "mime": "text/markdown", "size": 4,
                     "url": "/api/files/7-2?sign=abc"}]


class _FakeHttpResp:
    def __init__(self, content, headers=None):
        self.content = content
        self.headers = headers or {"Content-Length": str(len(content))}

    def raise_for_status(self):
        pass


class _FakeHttpClient:
    def __init__(self, content_by_url):
        self._by_url = content_by_url
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        return _FakeHttpResp(self._by_url[url])

    def close(self):
        pass


def test_youtrack_board_normalize_downloads_attachment():
    board = YouTrackBoard.__new__(YouTrackBoard)   # обойти httpx.Client в __init__
    board._key_pattern = KP
    board._base = BASE
    board._att_domains = ("youtrack.cloud",)
    board._att_max_bytes = 10 * 1024 * 1024
    board._att_timeout = 10.0
    board._att_store_chars = 200000
    board._client = _FakeHttpClient(
        {"https://c.youtrack.cloud/api/files/7-2?sign=abc": b"# \xd0\xa1\xd0\xbf\xd0\xb5\xd0\xba\xd0\xb0\n\xd1\x82\xd0\xb5\xd0\xba\xd1\x81\xd1\x82"})
    raw = _issue_to_raw(_issue(attachments=[
        {"name": "spec.md", "mimeType": "text/markdown", "size": 13,
         "url": "/api/files/7-2?sign=abc"}]))
    brief = board.normalize(raw)
    assert brief["attachments"] == [{"name": "spec.md", "mime_type": "text/markdown",
                                     "size": 13, "content_text": "# Спека\nтекст"}]
    # скачано по полному origin+url (без Bearer-зависимости — sign в url):
    assert board._client.requested == ["https://c.youtrack.cloud/api/files/7-2?sign=abc"]


def test_youtrack_board_normalize_skips_offhost_attachment():
    """Off-host абсолютный URL вложения не скачивается (защита токена/SSRF, PRI-196)."""
    abs_url = "https://files.example.com/f/7-2?sign=abc"
    board = YouTrackBoard.__new__(YouTrackBoard)
    board._key_pattern = KP
    board._base = BASE
    board._att_domains = ("youtrack.cloud",)
    board._att_max_bytes = 10 * 1024 * 1024
    board._att_timeout = 10.0
    board._att_store_chars = 200000
    board._client = _FakeHttpClient({})   # пустой — если запрос произойдёт, будет KeyError
    raw = _issue_to_raw(_issue(attachments=[
        {"name": "doc.md", "mimeType": "text/markdown", "size": 12, "url": abs_url}]))
    brief = board.normalize(raw)
    assert board._client.requested == []      # off-host не запрашивался
    assert brief["attachments"] == []         # вложение пропущено


def test_youtrack_selfhosted_with_port_allows_own_attachment():
    """Self-hosted YouTrack на нестандартном порту качает свои же вложения (PRI-196)."""
    from urllib.parse import urlsplit
    from reviewer.tasks.boards.attachments import _registrable_domain
    base = "https://youtrack.example.com:8443/api"
    board = YouTrackBoard.__new__(YouTrackBoard)
    board._key_pattern = KP
    board._base = base
    board._att_domains = (_registrable_domain(urlsplit(base).netloc.split("@")[-1].split(":")[0]),)
    board._att_max_bytes = 10 * 1024 * 1024
    board._att_timeout = 10.0
    board._att_store_chars = 200000
    url = "https://youtrack.example.com:8443/api/files/7-2?sign=abc"
    board._client = _FakeHttpClient({url: "ТЗ".encode()})
    raw = _issue_to_raw(_issue(attachments=[
        {"name": "spec.md", "mimeType": "text/markdown", "size": 4,
         "url": "/api/files/7-2?sign=abc"}]))
    brief = board.normalize(raw)
    assert board._client.requested == [url]            # свой хост — скачан
    assert brief["attachments"][0]["content_text"] == "ТЗ"
