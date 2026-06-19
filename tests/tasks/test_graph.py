import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore
from reviewer.tasks.graph import PRRef, TaskGraph


@pytest.fixture
def graph_store():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    yield g
    g.close()


@pytest.fixture
def task_graph(graph_store):
    return TaskGraph(graph_store.driver)


class _FakeDriver:
    def __init__(self, records=None):
        self.calls = []
        self._records = records if records is not None else []

    def execute_query(self, query, **params):
        self.calls.append((query, params))
        return (self._records, None, None)


def test_upsert_task_codes_are_key_plus_aliases_deduped():
    d = _FakeDriver()
    TaskGraph(d).upsert_task("ID-1", ["PRI-2", "ID-1"], "T", "Open", "u")
    _query, params = d.calls[0]
    assert params["key"] == "ID-1"
    assert params["codes"] == ["ID-1", "PRI-2"]  # key first, self-alias dropped


def test_upsert_links_filters_keyless_and_counts():
    d = _FakeDriver()
    n = TaskGraph(d).upsert_links("ID-1", [
        {"key": "ID-2", "title": "child", "type": "subtask"},
        {"title": "no key"},  # dropped
    ])
    assert n == 1
    _query, params = d.calls[0]
    assert params["rows"] == [{"key": "ID-2", "title": "child", "type": "subtask"}]


def test_upsert_links_empty_does_not_query():
    d = _FakeDriver()
    assert TaskGraph(d).upsert_links("ID-1", []) == 0
    assert d.calls == []


def test_link_pr_params():
    d = _FakeDriver()
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="abc")
    TaskGraph(d).link_pr("ID-1", pr, ["a.py#foo", "b.py#bar"])
    _query, params = d.calls[0]
    assert params["key"] == "ID-1"
    assert params["pid"] == "o/r#7"
    assert params["repo"] == "o/r" and params["number"] == 7 and params["sha"] == "abc"
    assert params["touched"] == ["a.py#foo", "b.py#bar"]


def test_link_pr_sha_set_is_conditional():
    """sha проставляется условно: пустой sha не должен затирать существующий."""
    d = _FakeDriver()
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="")
    TaskGraph(d).link_pr("ID-1", pr, [])
    query, params = d.calls[0]
    assert "CASE WHEN $sha" in query
    assert params["sha"] == ""


def test_task_context_parses_record():
    rec = {
        "key": "ID-1", "title": "T", "status": "Open", "url": "u",
        "prs": [{"id": "o/r#7", "url": "pr", "sha": "abc", "touched": ["a.py#foo"]}],
        "linked": [{"key": "ID-2", "title": "child", "status": "Done",
                    "type": "subtask", "prs": [{"id": "o/r#8", "url": "pr8"}]}],
    }
    ctx = TaskGraph(_FakeDriver([rec])).task_context("ID-1")
    assert ctx["key"] == "ID-1"
    assert ctx["prs"][0]["touched"] == ["a.py#foo"]
    assert ctx["linked"][0]["type"] == "subtask"


def test_task_context_empty_when_no_record():
    assert TaskGraph(_FakeDriver([])).task_context("ZZ-9") == {}


def test_task_context_dedups_linked_by_key_and_type():
    rec = {
        "key": "ID-1", "title": "T", "status": "Open", "url": "u", "prs": [],
        "linked": [
            {"key": "ID-2", "title": "child", "status": None, "type": "subtask", "prs": []},
            # дубль того же соседа (взаимное ребро) — схлопывается
            {"key": "ID-2", "title": "child", "status": None, "type": "subtask", "prs": []},
            # тот же key, но другой type — остаётся
            {"key": "ID-2", "title": "child", "status": None, "type": "relates", "prs": []},
        ],
    }
    ctx = TaskGraph(_FakeDriver([rec])).task_context("ID-1")
    assert [(n["key"], n["type"]) for n in ctx["linked"]] == [
        ("ID-2", "subtask"), ("ID-2", "relates")]


@pytest.mark.integration
def test_link_pr_scopes_touched_symbol_by_repo(task_graph, graph_store):
    """TOUCHES создаёт :Symbol с repo=pr.repo — изоляция по репозиторию работает."""
    graph_store.init_schema()
    graph_store.clear()
    pr = PRRef(repo="a/x", number=1, url="u", sha="s")
    task_graph.link_pr("ID-1", pr, ["m.py#foo"])
    assert graph_store.find_symbol("a/x", "foo") == ["m.py#foo"]
    assert graph_store.find_symbol("b/y", "foo") == []
