import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.fixture
def graph_store():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    yield g
    g.close()


@pytest.mark.integration
def test_upsert_and_expand(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#g", "a.py#f", "a.py#h"])
    graph_store.upsert_edges("test/repo", [
        ("a.py#g", "CALLS", "a.py#f"),
        ("a.py#h", "CALLS", "a.py#g"),
    ])
    related = graph_store.expand("test/repo", ["a.py#g"], hops=2)
    assert {"a.py#f", "a.py#h"} <= related


@pytest.mark.integration
def test_callers_directed(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#f", "a.py#g", "a.py#h"])
    # g вызывает f; h вызывает f
    graph_store.upsert_edges("test/repo", [
        ("a.py#g", "CALLS", "a.py#f"),
        ("a.py#h", "CALLS", "a.py#f"),
    ])
    callers = graph_store.callers("test/repo", ["a.py#f"])
    assert callers == {"a.py#g", "a.py#h"}
    assert graph_store.callers("test/repo", ["a.py#g"]) == set()   # g никто не вызывает


@pytest.mark.integration
def test_find_symbol_prefers_exact_suffix(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#run", "b.py#A.run", "c.py#runner"])
    ids = graph_store.find_symbol("test/repo", "run")
    # точное имя (#run, A.run) раньше, чем подстрока (runner)
    assert ids[0] in {"a.py#run", "b.py#A.run"}
    assert "a.py#run" in ids and "b.py#A.run" in ids
    assert ids.index("c.py#runner") > ids.index("a.py#run")
    assert ids.index("c.py#runner") > ids.index("b.py#A.run")


@pytest.mark.integration
def test_find_symbol_exact_not_evicted_by_many_substring_hits(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    # много узлов с подстрокой "run" + один точный
    graph_store.upsert_nodes(
        "test/repo",
        [f"pkg/mod{i}.py#runner{i}" for i in range(40)] + ["a.py#run"],
    )
    ids = graph_store.find_symbol("test/repo", "run")
    assert ids[0] == "a.py#run"   # точное не вытеснено и идёт первым


@pytest.mark.integration
def test_graph_isolates_by_repo(graph_store):
    graph_store.init_schema()
    graph_store.clear()  # global wipe
    graph_store.upsert_nodes("a/x", ["m.py#foo", "m.py#bar"])
    graph_store.upsert_edges("a/x", [("m.py#bar", "CALLS", "m.py#foo")])
    graph_store.upsert_nodes("b/y", ["m.py#foo"])
    assert graph_store.callers("a/x", ["m.py#foo"]) == {"m.py#bar"}
    assert graph_store.callers("b/y", ["m.py#foo"]) == set()
    assert graph_store.find_symbol("b/y", "bar") == []


@pytest.mark.integration
def test_incremental_methods_preserve_incoming_calls(graph_store):
    graph_store.clear()
    # unchanged caller u.py#caller -> a.py#foo ; a.py also has stale a.py#gone
    graph_store.upsert_nodes("a/x", ["a.py#foo", "a.py#gone", "u.py#caller"])
    graph_store.upsert_edges("a/x", [("u.py#caller", "CALLS", "a.py#foo"),
                                     ("a.py#foo", "CALLS", "a.py#gone")])
    # simulate incremental patch of a.py: new symbols {a.py#foo, a.py#bar}, gone removed
    old = graph_store.symbols_for_paths("a/x", ["a.py"])
    assert old == {"a.py#foo", "a.py#gone"}
    graph_store.delete_symbols("a/x", list(old - {"a.py#foo", "a.py#bar"}))  # drop gone
    graph_store.delete_outgoing_calls("a/x", ["a.py#foo", "a.py#bar"])
    graph_store.upsert_nodes("a/x", ["a.py#foo", "a.py#bar"])
    graph_store.upsert_edges("a/x", [("a.py#bar", "CALLS", "a.py#foo")])
    # incoming edge from unchanged caller preserved; new internal caller added:
    assert graph_store.callers("a/x", ["a.py#foo"]) == {"u.py#caller", "a.py#bar"}
    assert graph_store.find_symbol("a/x", "gone") == []
