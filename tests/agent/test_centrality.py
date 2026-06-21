from reviewer.agent.centrality import annotate_centrality
from reviewer.vcs.base import Finding
from reviewer.index.store import Retrieved


def _ret(node_id, start, end):
    path, fqn = node_id.split("#", 1)
    return Retrieved(node_id, path, fqn, "function", start, end, "", 0.0)


class _Graph:
    """Фейк графа: degree = {node_id: int}."""
    def __init__(self, degree):
        self._d = degree

    def in_degree(self, repo, ids, *, branch=""):
        return {nid: self._d[nid] for nid in ids if nid in self._d}


class _Store:
    """Фейк стора: fetch_nodes_at игнорирует ref, отдаёт заданные узлы по id."""
    def __init__(self, nodes):
        self._nodes = nodes

    def fetch_nodes_at(self, repo, node_ids, ref):
        return [n for n in self._nodes if n.node_id in node_ids]


def _f(file="a.py", line=5, **kw):
    d = dict(category="correctness", severity="high", file=file, line=line,
             side="RIGHT", message="m", suggestion=None, confidence=0.9)
    d.update(kw)
    return Finding(**d)


def test_maps_finding_to_enclosing_symbol():
    f = _f(line=5)
    annotate_centrality(
        [f], _Graph({"a.py#foo": 3}), _Store([_ret("a.py#foo", 1, 10)]),
        repo="r", branch="dev", changed_node_ids=["a.py#foo"], overlay_ref="pr:1")
    assert f.centrality == 3.0


def test_picks_narrowest_symbol_on_nesting():
    f = _f(line=5)
    nodes = [_ret("a.py#Cls", 1, 100), _ret("a.py#Cls.m", 4, 6)]   # вложенный метод уже
    annotate_centrality(
        [f], _Graph({"a.py#Cls": 9, "a.py#Cls.m": 2}), _Store(nodes),
        repo="r", branch="dev",
        changed_node_ids=["a.py#Cls", "a.py#Cls.m"], overlay_ref="pr:1")
    assert f.centrality == 2.0   # выбран самый узкий диапазон (метод), не класс


def test_miss_leaves_zero():
    f = _f(line=999)             # вне диапазона символа
    annotate_centrality(
        [f], _Graph({"a.py#foo": 3}), _Store([_ret("a.py#foo", 1, 10)]),
        repo="r", branch="dev", changed_node_ids=["a.py#foo"], overlay_ref="pr:1")
    assert f.centrality == 0.0


def test_fail_soft_no_graph():
    f = _f(line=5)
    annotate_centrality(
        [f], None, _Store([_ret("a.py#foo", 1, 10)]),
        repo="r", branch="dev", changed_node_ids=["a.py#foo"], overlay_ref="pr:1")
    assert f.centrality == 0.0
