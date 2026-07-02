import reviewer.mcp.service as svc_mod
from reviewer.mcp.service import MCPReviewService
from reviewer.tasks.boards.base import RawTask


class _FakeTaskService:
    def __init__(self):
        self.indexed = []

    def index_task(self, brief):
        self.indexed.append(brief)
        return {"key": brief.get("key"), "embedded": True, "warnings": []}


class _Provider:
    def __init__(self, raw=None):
        self.finished = None
        self.closed = False
        self.fetched = None
        self._raw = raw if raw is not None else RawTask(
            key="ID-10", project_code="PRI-10", title="T", description="d",
            status=None, subtask_ids=[], timestamp=1, completed=True)

    def finish(self, key, pr_url, *, note=None, mark_done=True, done_state=None,
               done_column=None):
        self.finished = (key, pr_url, note, mark_done, done_state, done_column)
        return {"key": key, "board_id": "u1", "done_set": True,
                "pr_link_added": True, "already_closed": False, "warnings": []}

    def fetch_one(self, key):
        self.fetched = key
        return self._raw

    def normalize(self, raw):
        return {"key": raw.key, "title": raw.title, "status": "done",
                "project": "PRI", "description": raw.description}

    def close(self):
        self.closed = True


class _Svc(MCPReviewService):
    """Обходим тяжёлый __init__: только settings + components.task_service."""
    def __init__(self, configured, task_service=None):
        self.settings = type("S", (), {
            "configured_board_types": staticmethod(lambda: configured)})()
        self.components = type("C", (), {
            "task_service": task_service or _FakeTaskService()})()


def test_finish_task_resolves_single_board(monkeypatch):
    prov = _Provider()
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t, status_field=None: prov)
    out = _Svc(["yougile"]).finish_task("PRI-10", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["board_type"] == "yougile"
    assert out["done_set"] is True
    assert prov.finished == ("PRI-10", "https://github.com/o/r/pull/7",
                             None, True, None, None)
    assert prov.closed is True


def test_finish_task_no_board_configured():
    out = _Svc([]).finish_task("PRI-10", "url")
    assert out["status"] == "error"
    assert "board_type" in out["reason"]


def test_finish_task_ambiguous_requires_board_type():
    out = _Svc(["yougile", "youtrack"]).finish_task("PRI-10", "url")
    assert out["status"] == "error"


def test_finish_task_explicit_board_type(monkeypatch):
    prov = _Provider()
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t, status_field=None: prov)
    out = _Svc(["yougile", "youtrack"]).finish_task(
        "TES-1", "url", board_type="youtrack", done_state="Done")
    assert out["status"] == "ok"
    assert out["board_type"] == "youtrack"
    assert prov.finished[4] == "Done"


def test_finish_task_threads_status_field_and_done_column(monkeypatch):
    prov = _Provider()
    seen = {}
    monkeypatch.setattr(
        svc_mod, "make_board_provider",
        lambda s, t, status_field=None: (seen.__setitem__("sf", status_field), prov)[1])
    _Svc(["youtrack"]).finish_task("TES-1", "url", board_type="youtrack",
                                   status_field="Stage", done_column="Готово")
    assert seen["sf"] == "Stage"
    assert prov.finished[5] == "Готово"   # done_column доехал до provider.finish


def test_finish_task_failsoft(monkeypatch):
    class Boom:
        def finish(self, *a, **k):
            raise RuntimeError("kaboom")

        def close(self):
            pass

    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t, status_field=None: Boom())
    out = _Svc(["yougile"]).finish_task("PRI-10", "url")
    assert out["status"] == "error"
    assert "kaboom" in out["reason"]


def test_finish_task_writes_through_to_store(monkeypatch):
    # После записи в доску закрытая задача сразу переиндексируется в стор reviewer
    # (без гонки с watermark инкрементального sync_board).
    prov = _Provider()
    ts = _FakeTaskService()
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t, status_field=None: prov)
    out = _Svc(["yougile"], task_service=ts).finish_task(
        "PRI-10", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["reindexed"] is True
    assert prov.fetched == "PRI-10"
    assert len(ts.indexed) == 1
    assert ts.indexed[0]["key"] == "ID-10"
    assert ts.indexed[0]["status"] == "done"


def test_finish_task_writethrough_failsoft_when_fetch_none(monkeypatch):
    # fetch_one вернул None (сбой доски) → finish всё равно ok, reindexed=False,
    # стор не трогается (fallback — обычный sync_board).
    class _NoRaw(_Provider):
        def fetch_one(self, key):
            self.fetched = key
            return None

    prov = _NoRaw()
    ts = _FakeTaskService()
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t, status_field=None: prov)
    out = _Svc(["yougile"], task_service=ts).finish_task("PRI-10", "url")
    assert out["status"] == "ok"
    assert out["reindexed"] is False
    assert ts.indexed == []
