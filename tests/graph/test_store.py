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
def test_outgoing_neighbors_is_directed(graph_store):
    """Только исходящие рёбра: контекстное ядро отвечает на вопрос «что читать,
    чтобы НАПИСАТЬ», а не «кого я сломаю» — это разные вопросы."""
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#g", "a.py#f", "a.py#h"])
    graph_store.upsert_edges("test/repo", [
        ("a.py#g", "CALLS", "a.py#f"),
        ("a.py#h", "CALLS", "a.py#g"),
    ])
    out = graph_store.outgoing_neighbors("test/repo", ["a.py#g"])
    assert out == {"a.py#f"}


@pytest.mark.integration
def test_outgoing_neighbors_includes_implements(graph_store):
    """IMPLEMENTS идёт наследник→база: соседний адаптер и контракт — ровно тот
    случай, ради которого метрика заведена."""
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#Child", "b.py#Base"])
    graph_store.upsert_edges("test/repo", [
        ("a.py#Child", "IMPLEMENTS", "b.py#Base"),
    ])
    out = graph_store.outgoing_neighbors("test/repo", ["a.py#Child"])
    assert out == {"b.py#Base"}


@pytest.mark.integration
def test_outgoing_neighbors_empty_ids(graph_store):
    assert graph_store.outgoing_neighbors("test/repo", []) == set()


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


@pytest.mark.integration
def test_incremental_methods_preserve_incoming_implements(graph_store):
    """Minor N2 (ре-ревью): парный тест к test_incremental_methods_preserve_incoming_calls
    на живом Neo4j — delete_outgoing_implements сносит только ИСХОДЯЩИЕ IMPLEMENTS
    патчируемых узлов, входящие (от неизменённых файлов-наследников) уцелевают."""
    graph_store.clear()
    # unchanged sibling s.py#Sibling наследует a.py#Base; a.py также содержит
    # протухшую базу a.py#OldBase, от которой self-heal должен отвязать Base.
    graph_store.upsert_nodes("a/x", ["a.py#Base", "a.py#OldBase", "s.py#Sibling"])
    graph_store.upsert_edges("a/x", [("s.py#Sibling", "IMPLEMENTS", "a.py#Base"),
                                     ("a.py#Base", "IMPLEMENTS", "a.py#OldBase")])
    # simulate incremental patch of a.py: Base больше не наследует OldBase,
    # зато сам наследует новый a.py#NewBase.
    graph_store.delete_outgoing_implements("a/x", ["a.py#Base"])
    graph_store.upsert_nodes("a/x", ["a.py#NewBase"])
    graph_store.upsert_edges("a/x", [("a.py#Base", "IMPLEMENTS", "a.py#NewBase")])
    # входящее ребро от неизменённого файла-наследника уцелело:
    assert graph_store.implementations_detailed("a/x", ["a.py#Base"]) == [
        {"id": "s.py#Sibling", "rel": "IMPLEMENTS"}]
    # исходящее протухшее ребро снесено, свежее исходящее на месте:
    assert graph_store.bases_of("a/x", ["a.py#Base"]) == {"a.py#Base": ["a.py#NewBase"]}


@pytest.mark.integration
def test_callers_detailed_returns_rel_and_id(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["a.py#f", "a.py#g", "a.py#h"])
    graph_store.upsert_edges("test/repo", [
        ("a.py#g", "CALLS", "a.py#f"),
        ("a.py#h", "CALLS", "a.py#f"),
    ])
    out = graph_store.callers_detailed("test/repo", ["a.py#f"])
    assert out == [
        {"id": "a.py#g", "rel": "CALLS"},
        {"id": "a.py#h", "rel": "CALLS"},
    ]
    assert graph_store.callers_detailed("test/repo", ["a.py#g"]) == []


@pytest.mark.integration
def test_implementations_detailed_returns_rel_and_id(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes(
        "test/repo", ["base.py#Base", "impl.py#Sub", "impl.py#Other"])
    graph_store.upsert_edges("test/repo", [
        ("impl.py#Sub", "IMPLEMENTS", "base.py#Base"),
        ("impl.py#Other", "IMPLEMENTS", "base.py#Base"),
    ])
    out = graph_store.implementations_detailed("test/repo", ["base.py#Base"])
    assert out == [
        {"id": "impl.py#Other", "rel": "IMPLEMENTS"},
        {"id": "impl.py#Sub", "rel": "IMPLEMENTS"},
    ]
    # символ без наследников → пусто
    assert graph_store.implementations_detailed("test/repo", ["impl.py#Sub"]) == []


@pytest.mark.integration
def test_implementations_detailed_method_overrides(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes("test/repo", ["base.py#Base.run", "impl.py#Sub.run"])
    graph_store.upsert_edges("test/repo", [
        ("impl.py#Sub.run", "IMPLEMENTS", "base.py#Base.run"),
    ])
    out = graph_store.implementations_detailed("test/repo", ["base.py#Base.run"])
    assert out == [{"id": "impl.py#Sub.run", "rel": "IMPLEMENTS"}]


@pytest.mark.integration
def test_expand_detailed_rels_distance_and_self_excluded(graph_store):
    graph_store.init_schema()
    graph_store.clear()
    graph_store.upsert_nodes(
        "test/repo", ["base.py#A", "impl.py#B", "call.py#C", "deep.py#D"])
    graph_store.upsert_edges("test/repo", [
        ("impl.py#B", "IMPLEMENTS", "base.py#A"),   # B -[IMPLEMENTS]-> A  (d1)
        ("call.py#C", "CALLS", "base.py#A"),        # C -[CALLS]-> A       (d1)
        ("deep.py#D", "CALLS", "call.py#C"),        # D -[CALLS]-> C       (A..D = d2)
    ])
    out = graph_store.expand_detailed("test/repo", ["base.py#A"], hops=2)
    by_id = {r["id"]: r for r in out}
    assert "base.py#A" not in by_id                  # self исключён
    assert by_id["impl.py#B"]["dist"] == 1 and by_id["impl.py#B"]["rels"] == ["IMPLEMENTS"]
    assert by_id["call.py#C"]["dist"] == 1 and by_id["call.py#C"]["rels"] == ["CALLS"]
    assert by_id["deep.py#D"]["dist"] == 2
    # порядок — по (dist, id): d1 раньше d2
    assert [r["id"] for r in out][-1] == "deep.py#D"
