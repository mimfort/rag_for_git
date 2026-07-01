from reviewer.tools.graph_format import format_neighbors
from reviewer.index.store import Retrieved


class EchoStore:
    """fetch_nodes возвращает Retrieved для каждого запрошенного id (path:10)."""
    def __init__(self, known=None):
        self.known = known  # None => все известны; set => только эти
        self.last = None

    def fetch_nodes(self, repo, ids, overlay_ref, changed_paths, *, base_ref="base"):
        self.last = dict(repo=repo, ids=list(ids), overlay_ref=overlay_ref,
                         changed_paths=list(changed_paths), base_ref=base_ref)
        out = []
        for i in ids:
            if self.known is not None and i not in self.known:
                continue
            name = i.split("#", 1)[1]
            out.append(Retrieved(i, i.split("#", 1)[0], name, "function",
                                 10, 12, f"def {name}():\n    return 1", 0.0))
        return out


def test_empty_returns_empty_msg():
    out = format_neighbors([], store=EchoStore(), repo="a/b", branch="main",
                           overlay_ref=None, changed_paths=[], empty_msg="(нет связей)")
    assert out == "(нет связей)"


def test_callers_format_has_fileline_snippet_and_rel():
    out = format_neighbors(
        [{"id": "x.py#caller", "rel": "CALLS"}],
        store=EchoStore(), repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")
    assert "// x.py#caller (x.py:10) [CALLS]" in out
    assert "def caller():" in out


def test_related_format_has_rels_and_distance():
    out = format_neighbors(
        [{"id": "i.py#B", "rels": ["IMPLEMENTS"], "dist": 1},
         {"id": "d.py#D", "rels": ["CALLS", "CALLS"], "dist": 2}],
        store=EchoStore(), repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(нет связей)")
    assert "// i.py#B (i.py:10) [IMPLEMENTS, d1]" in out
    assert "// d.py#D (d.py:10) [CALLS, d2]" in out   # повтор типа схлопнут


def test_missing_in_index_keeps_id_with_note():
    out = format_neighbors(
        [{"id": "gone.py#z", "rel": "CALLS"}],
        store=EchoStore(known=set()), repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")
    assert "// gone.py#z [CALLS] (вне индекса)" in out


def test_store_none_degrades_to_id_and_rel():
    out = format_neighbors(
        [{"id": "a.py#f", "rel": "CALLS"}],
        store=None, repo="a/b", branch="main",
        overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")
    assert out == "// a.py#f [CALLS]"


def test_cap_truncates_with_note():
    neighbors = [{"id": f"m.py#f{i}", "rel": "CALLS"} for i in range(30)]
    out = format_neighbors(neighbors, store=EchoStore(), repo="a/b", branch="main",
                           overlay_ref=None, changed_paths=[], empty_msg="x")
    assert "m.py#f24" in out and "m.py#f25" not in out
    assert "(…ещё 5, усечено)" in out


def test_fetch_nodes_called_with_base_ref_for_branch():
    store = EchoStore()
    format_neighbors([{"id": "a.py#f", "rel": "CALLS"}],
                     store=store, repo="a/b", branch="dev",
                     overlay_ref="pr:9", changed_paths=["a.py"], empty_msg="x")
    assert store.last["base_ref"] == "base:dev"
    assert store.last["overlay_ref"] == "pr:9"
    assert store.last["changed_paths"] == ["a.py"]


def test_store_failure_degrades_and_logs_warning(caplog):
    import logging

    class BoomStore:
        def fetch_nodes(self, repo, ids, overlay_ref, changed_paths, *, base_ref="base"):
            raise RuntimeError("postgres down")

    with caplog.at_level(logging.WARNING, logger="reviewer.tools.graph_format"):
        out = format_neighbors([{"id": "a.py#f", "rel": "CALLS"}],
                               store=BoomStore(), repo="a/b", branch="main",
                               overlay_ref=None, changed_paths=[], empty_msg="x")
    # fail-open: рендер не падает, дегрейд к '(вне индекса)'
    assert out == "// a.py#f [CALLS] (вне индекса)"
    # дегрейд залогирован, а не проглочен молча
    assert any("fetch_nodes" in r.message and "postgres down" in r.message
               for r in caplog.records)


def test_blank_text_renders_header_only():
    class BlankStore:
        def fetch_nodes(self, repo, ids, overlay_ref, changed_paths, *, base_ref="base"):
            return [Retrieved(i, i.split("#", 1)[0], i.split("#", 1)[1], "function",
                              7, 8, "   \n  ", 0.0) for i in ids]
    out = format_neighbors([{"id": "a.py#f", "rel": "CALLS"}],
                           store=BlankStore(), repo="a/b", branch="main",
                           overlay_ref=None, changed_paths=[], empty_msg="x")
    assert out == "// a.py#f (a.py:7) [CALLS]"


def test_format_neighbors_respects_cap_param():
    neighbors = [{"id": f"a.py#f{i}", "rel": "CALLS"} for i in range(10)]
    out = format_neighbors(neighbors, store=None, repo="a/x", branch="dev",
                           overlay_ref="__none__", changed_paths=[], empty_msg="—", cap=3)
    assert out.count("// ") == 3
    assert "(…ещё 7, усечено)" in out
