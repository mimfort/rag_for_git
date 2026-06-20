from reviewer.tools.impact import extract_signature
from reviewer.tools.impact import (
    compute_impact, format_impact, ImpactItem, CallerRef,
)
from reviewer.index.store import Retrieved


def _ret(node_id, text):
    path, fqn = node_id.split("#", 1)
    return Retrieved(node_id, path, fqn, "function", 10, 20, text, 0.0)


class _Graph:
    def __init__(self, callers_map):
        self._c = callers_map

    def callers(self, repo, ids, *, branch=""):
        out = set()
        for nid in ids:
            out |= set(self._c.get(nid, []))
        return out


class _Store:
    """Фейк: by_ref = {ref: {node_id: text}}."""
    def __init__(self, by_ref):
        self._by_ref = by_ref

    def fetch_nodes_at(self, repo, node_ids, ref):
        m = self._by_ref.get(ref, {})
        return [_ret(nid, m[nid]) for nid in node_ids if nid in m]

    def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
        m = self._by_ref.get(base_ref, {})
        return [_ret(nid, m[nid]) for nid in node_ids if nid in m]


def test_extract_signature_single_line():
    assert extract_signature("def f(a, b):\n    return a") == "def f(a, b):"


def test_extract_signature_with_annotations():
    text = "def f(a: int, b: str = 'x') -> bool:\n    ..."
    assert extract_signature(text) == "def f(a: int, b: str = 'x') -> bool:"


def test_extract_signature_multiline():
    text = "def f(\n    a,\n    b,\n):\n    return a"
    assert extract_signature(text) == "def f( a, b, ):"


def test_extract_signature_async_and_decorator():
    text = "@cache\nasync def f(x):\n    return x"
    assert extract_signature(text) == "async def f(x):"


def test_extract_signature_class():
    assert extract_signature("class A(B, C):\n    pass") == "class A(B, C):"


def test_extract_signature_none_when_absent():
    assert extract_signature("x = 1\ny = 2") is None


def test_compute_impact_flags_external_callers_on_signature_change():
    graph = _Graph({"svc.py#f": ["a.py#g", "b.py#h", "svc.py#local"]})
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b, c):\n    ..."},
        "base:dev": {
            "svc.py#f": "def f(a, b):\n    ...",
            "a.py#g": "def g():\n    f(1, 2)",
            "b.py#h": "def h():\n    f(1, 2)",
        },
    })
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#f"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert len(items) == 1
    it = items[0]
    assert it.node_id == "svc.py#f"
    assert it.old_sig == "def f(a, b):"
    assert it.new_sig == "def f(a, b, c):"
    assert {c.path for c in it.callers} == {"a.py", "b.py"}  # svc.py#local в диффе → отфильтрован
    assert all(c.line == 10 for c in it.callers)


def test_compute_impact_gate_skips_body_only_change():
    graph = _Graph({"svc.py#f": ["a.py#g"]})
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b):\n    return a + b"},
        "base:dev": {"svc.py#f": "def f(a, b):\n    return a - b"},
    })
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#f"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert items == []


def test_compute_impact_skips_added_symbol():
    graph = _Graph({"svc.py#new": ["a.py#g"]})
    store = _Store({"pr:1": {"svc.py#new": "def new(a):\n    ..."}})  # нет base-версии
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#new"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert items == []


def test_compute_impact_no_external_callers_skipped():
    graph = _Graph({"svc.py#f": ["svc.py#local"]})  # вызывающий в том же изменённом файле
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b, c):\n    ..."},
        "base:dev": {"svc.py#f": "def f(a, b):\n    ..."},
    })
    items = compute_impact(graph, store, repo="r", branch="dev",
                           changed_node_ids=["svc.py#f"], changed_paths=["svc.py"],
                           overlay_ref="pr:1")
    assert items == []


def test_format_impact_renders_callers():
    items = [ImpactItem("svc.py#f", "def f(a):", "def f(a, b):",
                        [CallerRef("a.py#g", "a.py", 10, "def g():")])]
    out = format_impact(items)
    assert "svc.py#f" in out and "def f(a, b):" in out and "a.py:10" in out


def test_format_impact_empty():
    assert "не найдено" in format_impact([])


def test_get_impact_tool_registered_and_runs():
    from reviewer.tools.code_tools import make_tools, ToolContext
    graph = _Graph({"svc.py#f": ["a.py#g"]})
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b, c):\n    ..."},
        "base:dev": {"svc.py#f": "def f(a, b):\n    ...", "a.py#g": "def g():\n    f(1, 2)"},
    })
    ctx = ToolContext(retriever=None, graph=graph, overlay_ref="pr:1",
                      changed_paths=["svc.py"], changed_node_ids=["svc.py#f"],
                      repo="r", branch="dev", store=store)
    tools = {t.name: t for t in make_tools(ctx)}
    assert "get_impact" in tools
    out = tools["get_impact"].invoke({})
    assert "a.py:10" in out and "def f(a, b, c):" in out


def test_get_impact_tool_no_graph():
    from reviewer.tools.code_tools import make_tools, ToolContext
    ctx = ToolContext(retriever=None, graph=None, overlay_ref="pr:1",
                      changed_paths=[], changed_node_ids=[], repo="r", branch="dev")
    tools = {t.name: t for t in make_tools(ctx)}
    assert "недоступн" in tools["get_impact"].invoke({})
