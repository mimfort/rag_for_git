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
    # изолируем резолв repo/ветки и depth от .env / сети
    svc._resolve_repo_branch = lambda repo, branch: ("o/n", "dev")
    svc._resolve_summary_depth = lambda repo, branch: (2, {}, "env")
    svc._resolve_summary_topk_threshold = lambda repo, branch: (20, "env")
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
         "source_hash": sh, "updated_at": "2026-06-23T00:00:00+00:00"}]
    svc = _svc(c)

    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", sh)
    assert out == {"cluster_key": "reviewer/index", "stored": True, "members": 2}
    # upsert получил выведенный (отсортированный) member_node_ids, а не []
    args = c.summary_store.upsert_summary.call_args.args
    assert args[5] == ["reviewer/index/a.py#A", "reviewer/index/b.py#B"]

    got = svc.get_subsystem_summaries("o/n", "dev")
    assert got["summaries"][0]["cluster_key"] == "reviewer/index"


def test_get_subsystem_summaries_marks_fresh_hash():
    from reviewer.graph.summaries import compute_source_hash

    c = MagicMock()
    current = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
        "source_hash": current, "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is False


def test_get_subsystem_summaries_marks_mismatched_hash_stale():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
        "source_hash": "old", "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is True


def test_get_subsystem_summaries_marks_absent_current_cluster_stale():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
        "source_hash": "stored-index", "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.return_value = [
        ("reviewer/mcp/service.py", "MCPReviewService", "h1", 1, "sk1")
    ]

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is True


def test_get_subsystem_summaries_empty_base_has_unknown_freshness():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
        "source_hash": "stored-index", "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.return_value = []

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is None


def test_get_subsystem_summaries_derivation_failure_is_unknown():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
        "source_hash": "stored", "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.side_effect = RuntimeError("db down")

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is None


def test_get_subsystem_summaries_single_key_marks_stale():
    c = MagicMock()
    c.summary_store.get_summary.return_value = {
        "cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
        "source_hash": "old", "updated_at": "2026-06-23T00:00:00+00:00",
    }
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]

    out = _svc(c).get_subsystem_summaries(
        "o/n", "dev", cluster_key="reviewer/index"
    )

    assert out["summary"]["stale"] is True


def test_get_subsystem_summaries_empty_does_not_scan_base():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = []

    assert _svc(c).get_subsystem_summaries("o/n", "dev") == {"summaries": []}
    c.store.list_base_members.assert_not_called()


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


# ── тесты _resolve_summary_depth и depth/orphans в ответе list ──────────────

class _FakeVCS:
    def __init__(self, text):
        self._text = text

    def get_file_at_ref(self, path, ref):
        return self._text


def _svc_with_vcs(vcs_or_exc):
    """Сервис БЕЗ стаба _resolve_summary_depth — для проверки самого хелпера."""
    c = MagicMock()
    svc = MCPReviewService(_settings(), components=c)
    svc._resolve_repo_branch = lambda repo, branch: ("o/n", "dev")
    if isinstance(vcs_or_exc, Exception):
        def _factory(owner, name):
            raise vcs_or_exc
    else:
        def _factory(owner, name):
            return vcs_or_exc
    svc._vcs_factory = _factory
    return svc


def test_resolve_summary_depth_override_from_review_yml():
    svc = _svc_with_vcs(_FakeVCS("summary_cluster_depth: 3"))
    depth, overrides, source = svc._resolve_summary_depth("o/n", "dev")
    assert depth == 3
    assert overrides == {}
    assert source == ".review.yml"


def test_resolve_summary_depth_no_key_falls_back_to_env():
    svc = _svc_with_vcs(_FakeVCS("severity_threshold: high"))
    depth, overrides, source = svc._resolve_summary_depth("o/n", "dev")
    assert depth == svc.settings.summary_cluster_depth
    assert overrides == {}
    assert source == "env"


def test_resolve_summary_depth_failsoft_on_vcs_error():
    svc = _svc_with_vcs(RuntimeError("no token"))
    depth, overrides, source = svc._resolve_summary_depth("o/n", "dev")
    assert depth == svc.settings.summary_cluster_depth
    assert overrides == {}
    assert source == "env"


def test_list_subsystem_clusters_reports_depth_and_orphans():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    c.graph = None
    # хранится осиротевший ключ reviewer/old (его нет среди текущих кластеров) → orphans=1
    c.summary_store.get_source_hashes.return_value = {"reviewer/old": "x"}
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    assert out["depth"] == 2
    assert out["depth_source"] == "arg"           # передан явный depth
    assert out["orphans"] == 1


def test_list_subsystem_clusters_resolves_depth_when_not_given():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    svc = _svc(c)                                  # стаб _resolve_summary_depth → (2, "env")
    out = svc.list_subsystem_clusters("o/n", "dev")   # depth не передан
    assert out["depth"] == 2
    assert out["depth_source"] == "env"
    assert out["orphans"] == 0


def test_prune_subsystem_summaries_rederives_keys_and_prunes():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"),
        ("b/y/g.py", "G", "h", 1, "skg"),
    ]
    c.summary_store.delete_summaries_except.return_value = 2
    svc = _svc(c)                                  # стаб _resolve_summary_depth → (2, "env")
    out = svc.prune_subsystem_summaries("o/n", "dev")
    assert out == {"pruned": 2, "kept": 2}
    # keep_keys пере-выведены на depth=2 и отсортированы
    args = c.summary_store.delete_summaries_except.call_args.args
    assert args[0] == "o/n" and args[1] == "dev"
    assert args[2] == ["a/x", "b/y"]


def test_prune_subsystem_summaries_empty_base_is_noop():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    svc = _svc(c)
    out = svc.prune_subsystem_summaries("o/n", "dev")
    assert out["pruned"] == 0
    assert "note" in out
    c.summary_store.delete_summaries_except.assert_not_called()   # base пуст → не вайпаем


def test_index_subsystem_summary_embeds_when_hash_changed():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {}          # сводки ещё нет → hash изменился
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    svc = _svc(c)
    svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "тело", sh)
    c.embedder.embed_documents.assert_called_once_with(["Индекс\nтело"])
    assert c.summary_store.upsert_summary.call_args.kwargs["embedding"] == [0.5, 0.5]


def test_index_subsystem_summary_dedups_embedding_on_unchanged_hash():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": sh}   # хеш совпал
    svc = _svc(c)
    svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "тело", sh)
    c.embedder.embed_documents.assert_not_called()              # Voyage не дёрнут
    assert c.summary_store.upsert_summary.call_args.kwargs["embedding"] is None


def test_resolve_summary_topk_threshold_override_from_review_yml():
    svc = _svc_with_vcs(_FakeVCS("summary_topk_threshold: 5"))
    assert svc._resolve_summary_topk_threshold("o/n", "dev") == (5, ".review.yml")


def test_resolve_summary_topk_threshold_no_key_falls_back_to_env():
    svc = _svc_with_vcs(_FakeVCS("severity_threshold: high"))
    val, source = svc._resolve_summary_topk_threshold("o/n", "dev")
    assert val == svc.settings.summary_topk_threshold
    assert source == "env"


def test_settings_default_summary_topk_threshold_is_20():
    assert _settings().summary_topk_threshold == 20


def test_get_subsystem_summaries_query_above_threshold_returns_topk():
    c = MagicMock()
    c.summary_store.count_summaries.return_value = 25            # > порога 20
    c.summary_store.search_summaries.return_value = [
        {"cluster_key": "auth", "title": "Авторизация", "summary": "...",
         "source_hash": "stored", "updated_at": "2026-06-23T00:00:00+00:00"}]
    c.embedder.embed_query.return_value = [0.1, 0.2]
    svc = _svc(c)
    out = svc.get_subsystem_summaries("o/n", "dev", query="как работает логин")
    assert out["summaries"][0]["cluster_key"] == "auth"
    assert out["summaries"][0]["stale"] is None
    c.embedder.embed_query.assert_called_once_with("как работает логин")
    assert c.summary_store.search_summaries.call_args.args[3] == 8   # top_k по умолчанию
    c.summary_store.get_summaries.assert_not_called()
    c.store.list_base_members.assert_not_called()


def test_get_subsystem_summaries_query_below_threshold_returns_all():
    from reviewer.graph.summaries import compute_source_hash

    c = MagicMock()
    c.summary_store.count_summaries.return_value = 5             # ≤ порога 20
    current = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_summaries.return_value = [
        {"cluster_key": "reviewer/index", "title": "Индекс", "summary": "...",
         "source_hash": current, "updated_at": "2026-06-23T00:00:00+00:00"}]
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]
    svc = _svc(c)
    out = svc.get_subsystem_summaries("o/n", "dev", query="что угодно")
    assert out["summaries"][0]["cluster_key"] == "reviewer/index"
    assert out["summaries"][0]["stale"] is False
    c.summary_store.search_summaries.assert_not_called()        # бэк-компат: отдаём все
    c.embedder.embed_query.assert_not_called()                  # Voyage не дёрнут (ниже порога)


def test_get_subsystem_summaries_no_query_returns_all_without_counting():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = []
    svc = _svc(c)
    out = svc.get_subsystem_summaries("o/n", "dev")
    assert out == {"summaries": []}
    c.summary_store.search_summaries.assert_not_called()
    c.summary_store.count_summaries.assert_not_called()         # без query порог не считаем


def test_backfill_summary_embeddings_fills_pending():
    c = MagicMock()
    c.summary_store.get_pending_embeddings.return_value = [
        {"cluster_key": "auth", "title": "Авторизация", "summary": "тело"}]
    c.embedder.embed_documents.return_value = [[0.3, 0.4]]
    svc = _svc(c)
    out = svc.backfill_summary_embeddings("o/n", "dev")
    assert out == {"embedded": 1}
    c.embedder.embed_documents.assert_called_once_with(["Авторизация\nтело"])
    c.summary_store.set_embedding.assert_called_once_with("o/n", "dev", "auth", [0.3, 0.4])


def test_backfill_summary_embeddings_noop_when_none_pending():
    c = MagicMock()
    c.summary_store.get_pending_embeddings.return_value = []
    svc = _svc(c)
    out = svc.backfill_summary_embeddings("o/n", "dev")
    assert out == {"embedded": 0}
    c.embedder.embed_documents.assert_not_called()


def test_index_subsystem_summary_failsoft_when_embed_raises():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {}          # hash изменился → ветка эмбеддинга
    c.embedder.embed_documents.side_effect = RuntimeError("voyage down")
    svc = _svc(c)
    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "тело", sh)
    assert out["stored"] is True
    assert "note" in out                                         # fail-soft нота
    assert c.summary_store.upsert_summary.call_args.kwargs["embedding"] is None  # сводка сохранена без вектора


def test_backfill_summary_embeddings_failsoft_when_embed_raises():
    c = MagicMock()
    c.summary_store.get_pending_embeddings.return_value = [
        {"cluster_key": "auth", "title": "Авторизация", "summary": "тело"}]
    c.embedder.embed_documents.side_effect = RuntimeError("voyage down")
    svc = _svc(c)
    out = svc.backfill_summary_embeddings("o/n", "dev")
    assert out["embedded"] == 0
    assert "note" in out
    c.summary_store.set_embedding.assert_not_called()            # без вектора не пишем
