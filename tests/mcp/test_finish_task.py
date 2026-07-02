import reviewer.mcp.service as svc_mod
from reviewer.mcp.service import MCPReviewService


class _Provider:
    def __init__(self):
        self.finished = None
        self.closed = False

    def finish(self, key, pr_url, *, note=None, mark_done=True, done_state=None,
               done_column=None):
        self.finished = (key, pr_url, note, mark_done, done_state, done_column)
        return {"key": key, "board_id": "u1", "done_set": True,
                "pr_link_added": True, "already_closed": False, "warnings": []}

    def close(self):
        self.closed = True


class _Svc(MCPReviewService):
    """Обходим тяжёлый __init__: только settings с configured_board_types."""
    def __init__(self, configured):
        self.settings = type("S", (), {
            "configured_board_types": staticmethod(lambda: configured)})()


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
