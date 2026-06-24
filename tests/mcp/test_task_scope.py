from reviewer.mcp.service import MCPReviewService


class _FakeTaskService:
    def __init__(self):
        self.calls = {}

    def search_tasks(self, query, top_k=5, project=None):
        self.calls["search"] = (query, top_k, project)
        return "ok"

    def get_task_context(self, key, project=None):
        self.calls["context"] = (key, project)
        return "ok"

    def get_task(self, key, project=None):
        self.calls["get"] = (key, project)
        return {"key": key}


class _Svc(MCPReviewService):
    def __init__(self, task_service):
        self.components = type("C", (), {"task_service": task_service})()


def test_read_tools_thread_project():
    ts = _FakeTaskService()
    svc = _Svc(ts)
    svc.search_tasks("q", project="PRI")
    svc.get_task_context("ID-1", project="PRI")
    svc.get_task("ID-1", project="PRI")
    assert ts.calls["search"] == ("q", 5, "PRI")
    assert ts.calls["context"] == ("ID-1", "PRI")
    assert ts.calls["get"] == ("ID-1", "PRI")
