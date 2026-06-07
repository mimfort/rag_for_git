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
