import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore

@pytest.mark.integration
def test_upsert_and_expand():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema(); g.clear()
    g.upsert_nodes(["a.py#g", "a.py#f", "a.py#h"])
    g.upsert_edges([("a.py#g", "CALLS", "a.py#f"), ("a.py#h", "CALLS", "a.py#g")])
    related = g.expand(["a.py#g"], hops=2)
    assert {"a.py#f", "a.py#h"} <= related
    g.close()


@pytest.mark.integration
def test_callers_directed():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema(); g.clear()
    g.upsert_nodes(["a.py#f", "a.py#g", "a.py#h"])
    # g вызывает f; h вызывает f
    g.upsert_edges([("a.py#g", "CALLS", "a.py#f"), ("a.py#h", "CALLS", "a.py#f")])
    callers = g.callers(["a.py#f"])
    assert callers == {"a.py#g", "a.py#h"}
    assert g.callers(["a.py#g"]) == set()   # g никто не вызывает
    g.close()


@pytest.mark.integration
def test_find_symbol_prefers_exact_suffix():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema(); g.clear()
    g.upsert_nodes(["a.py#run", "b.py#A.run", "c.py#runner"])
    ids = g.find_symbol("run")
    # точное имя (#run, A.run) раньше, чем подстрока (runner)
    assert ids[0] in {"a.py#run", "b.py#A.run"}
    assert "a.py#run" in ids and "b.py#A.run" in ids
    assert ids.index("c.py#runner") > ids.index("a.py#run")
    assert ids.index("c.py#runner") > ids.index("b.py#A.run")
    g.close()


@pytest.mark.integration
def test_find_symbol_exact_not_evicted_by_many_substring_hits():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema(); g.clear()
    # много узлов с подстрокой "run" + один точный
    g.upsert_nodes([f"pkg/mod{i}.py#runner{i}" for i in range(40)] + ["a.py#run"])
    ids = g.find_symbol("run")
    assert ids[0] == "a.py#run"   # точное не вытеснено и идёт первым
    g.close()
