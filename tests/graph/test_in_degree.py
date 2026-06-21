import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_in_degree_counts_incoming_calls():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    g.init_schema()
    g.clear("a/x", branch="main")
    g.upsert_nodes("a/x", ["m.py#hub", "m.py#c1", "m.py#c2", "m.py#leaf"], branch="main")
    g.upsert_edges("a/x", [("m.py#c1", "CALLS", "m.py#hub"),
                           ("m.py#c2", "CALLS", "m.py#hub")], branch="main")
    try:
        deg = g.in_degree("a/x", ["m.py#hub", "m.py#leaf"], branch="main")
        assert deg.get("m.py#hub") == 2
        assert "m.py#leaf" not in deg          # нет вызывающих → ключ отсутствует
        assert g.in_degree("a/x", [], branch="main") == {}
    finally:
        g.clear("a/x", branch="main")
        g.close()
