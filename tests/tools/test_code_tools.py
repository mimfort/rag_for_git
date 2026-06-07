from reviewer.tools.code_tools import make_tools, ToolContext

class FakeRetriever:
    def retrieve(self, **kw):
        from reviewer.retrieval.retriever import ContextPack
        from reviewer.index.store import Retrieved
        return ContextPack([Retrieved("a.py#f","a.py","f","function",1,2,"def f(): ...",1.0)])
class FakeGraph:
    def expand(self, ids, hops=2): return {"b.py#g"}

def test_search_code_tool_returns_context_text():
    ctx = ToolContext(retriever=FakeRetriever(), graph=FakeGraph(),
                      overlay_ref="pr:1", changed_paths=["a.py"], changed_node_ids=[])
    tools = {t.name: t for t in make_tools(ctx)}
    out = tools["search_code"].invoke({"query": "where is f"})
    assert "a.py#f" in out

def test_get_callers_tool_uses_graph():
    ctx = ToolContext(retriever=FakeRetriever(), graph=FakeGraph(),
                      overlay_ref="pr:1", changed_paths=[], changed_node_ids=[])
    tools = {t.name: t for t in make_tools(ctx)}
    out = tools["get_related_symbols"].invoke({"node_id": "a.py#f"})
    assert "b.py#g" in out


class FakeGraphRich:
    def expand(self, ids, hops=2): return {"b.py#g"}
    def callers(self, ids): return {"x.py#caller"}
    def find_symbol(self, name): return ["a.py#f"]

class FakeStore:
    def fetch_nodes(self, ids, overlay_ref, changed_paths):
        from reviewer.index.store import Retrieved
        return [Retrieved("a.py#f", "a.py", "f", "function", 1, 2, "def f():\n    return 1", 0.0)]


def _rich_ctx(**over):
    base = dict(retriever=FakeRetriever(), graph=FakeGraphRich(),
                overlay_ref="pr:1", changed_paths=["a.py"], changed_node_ids=[],
                read_file_fn=lambda p: "l1\nl2\nl3" if p == "a.py" else None,
                patches={"a.py": "@@ -1 +1 @@\n-x\n+y"}, store=FakeStore())
    base.update(over)
    return ToolContext(**base)


def test_read_file_returns_numbered_slice():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["read_file"].invoke({"path": "a.py", "start": 1, "end": 2})
    assert "1|l1" in out and "2|l2" in out and "3|l3" not in out


def test_read_file_missing():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["read_file"].invoke({"path": "nope.py"})
    assert "не найден" in out


def test_get_definition_uses_graph_and_store():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["get_definition"].invoke({"symbol": "f"})
    assert "a.py#f" in out and "def f" in out


def test_find_callers_directed():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["find_callers"].invoke({"node_id": "a.py#f"})
    assert "x.py#caller" in out


def test_get_changed_file_diff_returns_patch():
    tools = {t.name: t for t in make_tools(_rich_ctx())}
    out = tools["get_changed_file_diff"].invoke({"path": "a.py"})
    assert "+y" in out
    out2 = tools["get_changed_file_diff"].invoke({"path": "other.py"})
    assert "не входит" in out2
