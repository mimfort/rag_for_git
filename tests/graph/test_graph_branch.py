import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_branch_isolation_in_graph():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear("a/x", branch="main")
    g.clear("a/x", branch="master")
    g.upsert_nodes("a/x", ["mod.py#a", "mod.py#b"], branch="main")
    g.upsert_edges("a/x", [("mod.py#a", "CALLS", "mod.py#b")], branch="main")
    g.upsert_nodes("a/x", ["mod.py#a", "mod.py#c"], branch="master")
    g.upsert_edges("a/x", [("mod.py#a", "CALLS", "mod.py#c")], branch="master")
    try:
        main_rel = g.expand("a/x", ["mod.py#a"], hops=1, branch="main")
        master_rel = g.expand("a/x", ["mod.py#a"], hops=1, branch="master")
        assert "mod.py#b" in main_rel and "mod.py#c" not in main_rel
        assert "mod.py#c" in master_rel and "mod.py#b" not in master_rel
    finally:
        g.clear("a/x", branch="main")
        g.clear("a/x", branch="master")
        g.close()
