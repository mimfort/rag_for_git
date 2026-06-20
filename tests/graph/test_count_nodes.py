import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_count_nodes_by_branch():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear("a/x", branch="main")
    g.upsert_nodes("a/x", ["m.py#a", "m.py#b"], branch="main")
    try:
        assert g.count_nodes("a/x", "main") == 2
        assert g.count_nodes("a/x", "absent") == 0
    finally:
        g.clear("a/x", branch="main")
        g.close()
