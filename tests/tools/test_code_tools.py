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


def test_read_file_caps_at_400_lines():
    big = "\n".join(f"x{i}" for i in range(1, 1001))   # 1000 строк
    tools = {t.name: t for t in make_tools(_rich_ctx(read_file_fn=lambda p: big))}
    out = tools["read_file"].invoke({"path": "a.py", "start": 1, "end": 1000})
    assert "1|x1" in out and "400|x400" in out
    assert "401|x401" not in out and "(…усечено)" in out


def test_read_file_subrange_within_file_has_no_truncation_marker():
    big = "\n".join(f"x{i}" for i in range(1, 101))    # 100 строк
    tools = {t.name: t for t in make_tools(_rich_ctx(read_file_fn=lambda p: big))}
    out = tools["read_file"].invoke({"path": "a.py", "start": 1, "end": 10})
    assert "10|x10" in out and "(…усечено)" not in out


def test_read_file_start_beyond_eof():
    tools = {t.name: t for t in make_tools(_rich_ctx())}   # файл a.py = 3 строки
    out = tools["read_file"].invoke({"path": "a.py", "start": 50})
    assert "нет строки 50" in out


class CountingRetriever:
    """Считает вызовы retrieve — для проверки, что кэш экономит обращения к источнику."""
    def __init__(self):
        self.calls = 0
    def retrieve(self, **kw):
        from reviewer.retrieval.retriever import ContextPack
        from reviewer.index.store import Retrieved
        self.calls += 1
        return ContextPack([Retrieved("a.py#f", "a.py", "f", "function", 1, 2, "def f(): ...", 1.0)])


def test_within_unit_duplicate_returns_stub():
    """Повтор того же (tool, args) в пределах юнита -> заглушка, источник дёрнут один раз."""
    ret = CountingRetriever()
    ctx = ToolContext(retriever=ret, graph=FakeGraph(),
                      overlay_ref="pr:1", changed_paths=["a.py"], changed_node_ids=[])
    tools = {t.name: t for t in make_tools(ctx)}
    out1 = tools["search_code"].invoke({"query": "q"})
    out2 = tools["search_code"].invoke({"query": "q"})
    assert "a.py#f" in out1
    assert out2 == "(повтор: результат уже показан выше)"
    assert ret.calls == 1


def test_run_cache_shared_across_units_same_node_ids():
    """Общий run-level кэш: второй юнит (новый make_tools) с тем же changed_node_ids
    берёт результат из кэша, не дёргая источник."""
    ret = CountingRetriever()
    cache: dict = {}
    ctx1 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=[], cache=cache)
    ctx2 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=[], cache=cache)
    t1 = {t.name: t for t in make_tools(ctx1)}
    t2 = {t.name: t for t in make_tools(ctx2)}
    t1["search_code"].invoke({"query": "q"})
    out = t2["search_code"].invoke({"query": "q"})
    assert "a.py#f" in out
    assert ret.calls == 1


def test_run_cache_respects_changed_node_ids():
    """Корректность: разные changed_node_ids -> разный ключ -> источник дёргается дважды."""
    ret = CountingRetriever()
    cache: dict = {}
    ctx1 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=["a.py#f"], cache=cache)
    ctx2 = ToolContext(retriever=ret, graph=FakeGraph(), overlay_ref="pr:1",
                       changed_paths=["a.py"], changed_node_ids=["b.py#g"], cache=cache)
    t1 = {t.name: t for t in make_tools(ctx1)}
    t2 = {t.name: t for t in make_tools(ctx2)}
    t1["search_code"].invoke({"query": "q"})
    t2["search_code"].invoke({"query": "q"})
    assert ret.calls == 2


def test_cache_normalizes_read_file_defaults():
    """read_file(path) и read_file(path, 1, 400) дают один ключ (apply_defaults) -> повтор = заглушка."""
    calls = {"n": 0}
    def rf(p):
        calls["n"] += 1
        return "l1\nl2\nl3"
    ctx = ToolContext(retriever=FakeRetriever(), graph=FakeGraph(), overlay_ref="pr:1",
                      changed_paths=["a.py"], changed_node_ids=[],
                      read_file_fn=rf, cache={})
    tools = {t.name: t for t in make_tools(ctx)}
    tools["read_file"].invoke({"path": "a.py"})
    tools["read_file"].invoke({"path": "a.py", "start": 1, "end": 400})
    assert calls["n"] == 1
