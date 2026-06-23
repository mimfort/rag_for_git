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
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 2, "sk2"),
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
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    c.graph = None
    # вычисляем эталонный source_hash так же, как продакшен
    from reviewer.graph.summaries import compute_source_hash
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])   # по skeleton_hash, не "h1"
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
    assert "branch" in out
    assert out["deferred"] == 0


def test_index_and_get_subsystem_summaries_roundtrip_via_store():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    # base-состав кластера reviewer/index (depth=2) — сервер выведет member_node_ids из него
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 2, "sk2"),
    ]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1"),
                              ("reviewer/index/b.py#B", "sk2")])
    c.summary_store.get_summaries.return_value = [
        {"cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
         "updated_at": "2026-06-23T00:00:00+00:00"}]
    svc = _svc(c)

    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", sh)
    assert out == {"cluster_key": "reviewer/index", "stored": True, "members": 2}
    # upsert получил выведенный (отсортированный) member_node_ids, а не []
    args = c.summary_store.upsert_summary.call_args.args
    assert args[5] == ["reviewer/index/a.py#A", "reviewer/index/b.py#B"]

    got = svc.get_subsystem_summaries("o/n", "dev")
    assert got["summaries"][0]["cluster_key"] == "reviewer/index"


def test_index_subsystem_summary_stale_hash_empties_members():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    svc = _svc(c)
    # передан неактуальный source_hash → пере-вычисленный не совпадёт
    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", "STALE")
    assert out["stored"] is True
    assert out["members"] == 0
    assert "note" in out
    assert c.summary_store.upsert_summary.call_args.args[5] == []   # member_node_ids пуст


def test_list_subsystem_clusters_cap_defers_lowest_priority():
    from datetime import datetime
    c = MagicMock()
    # три кластера-одиночки (depth=2 даёт разные ключи), все stale
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"),
        ("b/y/g.py", "G", "h", 1, "skg"),
        ("c/z/h.py", "H", "h", 1, "skh"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}          # все stale
    # a/x — без сводки (нет в updated); b/y старее c/z
    c.summary_store.get_updated_ats.return_value = {
        "b/y": datetime(2026, 1, 1), "c/z": datetime(2026, 6, 1)}
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", min_size=1, cap=2)
    keys = {cl["cluster_key"] for cl in out["clusters"]}
    assert out["deferred"] == 1
    assert keys == {"a/x", "b/y"}        # без сводки + старейший; c/z отложен


def test_list_subsystem_clusters_no_cap_returns_all():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"), ("b/y/g.py", "G", "h", 1, "skg")]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", min_size=1)   # cap=None
    assert out["deferred"] == 0
    assert len(out["clusters"]) == 2
    c.summary_store.get_updated_ats.assert_not_called()           # порядок не нужен без cap
