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
