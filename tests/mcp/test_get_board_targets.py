import reviewer.mcp.service as svc_mod
from reviewer.mcp.service import MCPReviewService


class _Provider:
    def __init__(self, targets):
        self.targets = targets
        self.project = "UNSET"
        self.closed = False

    def list_done_targets(self, project):
        self.project = project
        return self.targets

    def close(self):
        self.closed = True


class _Svc(MCPReviewService):
    """Обходим тяжёлый __init__: только settings с configured_board_types."""
    def __init__(self, configured):
        self.settings = type("S", (), {
            "configured_board_types": staticmethod(lambda: configured)})()


def test_get_board_targets_single_board_threads_project(monkeypatch):
    prov = _Provider({"columns": [{"title": "Готово", "id": "c1",
                                   "board_id": "b1", "board_title": "B"}], "warnings": []})
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: prov)
    out = _Svc(["yougile"]).get_board_targets(project="PRI")
    assert out["board_type"] == "yougile"
    assert out["project"] == "PRI"
    assert out["columns"][0]["title"] == "Готово"
    assert prov.project == "PRI"
    assert prov.closed is True
    # креды наружу не отдаются
    assert "api_key" not in out and "token" not in out


def test_get_board_targets_ambiguous_requires_type():
    out = _Svc(["yougile", "youtrack"]).get_board_targets()
    assert out["status"] == "error"
    assert "board_type" in out["reason"]


def test_get_board_targets_not_configured():
    out = _Svc([]).get_board_targets(board_type="youtrack")
    assert out["status"] == "error"


def test_get_board_targets_explicit_type(monkeypatch):
    prov = _Provider({"status_fields": [{"field": "Stage", "values": ["Готово"],
                                         "$type": "X"}], "source": "admin", "warnings": []})
    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: prov)
    out = _Svc(["yougile", "youtrack"]).get_board_targets(board_type="youtrack",
                                                          project="TES")
    assert out["board_type"] == "youtrack"
    assert out["source"] == "admin"
    assert prov.project == "TES"


def test_get_board_targets_failsoft(monkeypatch):
    class Boom:
        def list_done_targets(self, project):
            raise RuntimeError("kaboom")

        def close(self):
            pass

    monkeypatch.setattr(svc_mod, "make_board_provider", lambda s, t: Boom())
    out = _Svc(["yougile"]).get_board_targets()
    assert out["status"] == "error"
    assert "kaboom" in out["reason"]
