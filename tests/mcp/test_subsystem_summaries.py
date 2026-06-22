"""Unit-тесты MCP-методов community summaries (PRI-159). Фейки вместо Postgres/Neo4j."""
from __future__ import annotations

from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.default_repo = ""
    return s


def _svc(components) -> MCPReviewService:
    svc = MCPReviewService(_settings(), components)
    # изолируем резолв repo/ветки от REVIEW_BRANCHES в .env
    svc._resolve_repo_branch = lambda repo, branch: ("o/n", "dev")
    return svc


def test_list_subsystem_clusters_marks_stale():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1),
        ("reviewer/index/b.py", "B", "h2", 2),
    ]
    c.graph = None                                  # граф недоступен → fail-soft
    c.summary_store.get_source_hashes.return_value = {}   # ничего не сохранено → stale=True
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    [cl] = out["clusters"]
    assert cl["cluster_key"] == "reviewer/index"
    assert cl["num_members"] == 2
    assert cl["stale"] is True


def test_list_subsystem_clusters_fresh_when_hash_matches():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1)]
    c.graph = None
    # вычисляем эталонный source_hash так же, как продакшен
    from reviewer.graph.summaries import compute_source_hash
    sh = compute_source_hash([("reviewer/index/a.py#A", "h1")])
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": sh}
    svc = _svc(c)
    [cl] = svc.list_subsystem_clusters("o/n", "dev")["clusters"]
    assert cl["stale"] is False


def test_list_subsystem_clusters_empty_index_returns_note():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev")
    assert out["clusters"] == []
    assert "note" in out


def test_index_and_get_subsystem_summaries_roundtrip_via_store():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [
        {"cluster_key": "reviewer/index", "title": "Индекс", "summary": "..."}]
    svc = _svc(c)
    assert svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", "h1") == {
        "cluster_key": "reviewer/index", "stored": True}
    c.summary_store.upsert_summary.assert_called_once()
    got = svc.get_subsystem_summaries("o/n", "dev")
    assert got["summaries"][0]["cluster_key"] == "reviewer/index"
