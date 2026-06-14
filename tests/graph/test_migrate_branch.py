import pytest
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore


@pytest.mark.integration
def test_migrate_legacy_branch_sets_primary():
    s = Settings()
    g = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    # создать legacy-узел без branch (как старый upsert)
    g._driver.execute_query("MERGE (:Symbol {repo: 'a/x', id: 'legacy.py#f'})")
    try:
        g.migrate_legacy_branch("main")
        rec, _, _ = g._driver.execute_query(
            "MATCH (s:Symbol {repo:'a/x', id:'legacy.py#f'}) RETURN s.branch AS b")
        assert rec[0]["b"] == "main"
    finally:
        g._driver.execute_query("MATCH (s:Symbol {repo:'a/x', id:'legacy.py#f'}) DETACH DELETE s")
        g.close()
