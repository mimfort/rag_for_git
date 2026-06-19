from types import SimpleNamespace

from reviewer.retrieval.retriever import _dedupe_overlapping


def _it(path, start, end, node_id=None):
    return SimpleNamespace(path=path, start_line=start, end_line=end,
                           node_id=node_id or f"{path}#s{start}")


def test_dedupe_drops_method_nested_in_class():
    cls = _it("a.py", 1, 50, "a.py#Foo")
    method = _it("a.py", 10, 20, "a.py#Foo.bar")
    assert [x.node_id for x in _dedupe_overlapping([cls, method])] == ["a.py#Foo"]


def test_dedupe_drops_class_when_method_comes_first():
    method = _it("a.py", 10, 20, "a.py#Foo.bar")
    cls = _it("a.py", 1, 50, "a.py#Foo")
    # метод пришёл раньше, но самый широкий (класс) должен остаться
    assert [x.node_id for x in _dedupe_overlapping([method, cls])] == ["a.py#Foo"]


def test_dedupe_keeps_partial_overlap():
    a = _it("a.py", 10, 30)
    b = _it("a.py", 20, 40)
    assert _dedupe_overlapping([a, b]) == [a, b]


def test_dedupe_independent_per_path():
    a = _it("a.py", 1, 50)
    b = _it("b.py", 10, 20)
    assert _dedupe_overlapping([a, b]) == [a, b]


def test_dedupe_preserves_order_of_survivors():
    a = _it("a.py", 1, 5)
    b = _it("b.py", 1, 5)
    c = _it("c.py", 1, 5)
    assert _dedupe_overlapping([a, b, c]) == [a, b, c]
