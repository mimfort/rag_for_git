"""Unit-тесты MCP-методов community summaries (PRI-159). Фейки вместо Postgres/Neo4j."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


def _two_cluster_generation_state(
    *,
    completed_depth: int | None,
    fragment_depth: int | None,
):
    from reviewer.graph.summaries import (
        Member,
        compute_file_fingerprints,
        compute_layout_token,
        compute_source_hash,
    )

    raw = [
        ("a/x/a.py", "A", "h1", 1, "sk1"),
        ("b/y/b.py", "B", "h2", 1, "sk2"),
    ]
    members = [
        Member("a/x/a.py#A", "a/x/a.py", "h1", "sk1", 1),
        Member("b/y/b.py#B", "b/y/b.py", "h2", "sk2", 1),
    ]
    fingerprints = compute_file_fingerprints(members)
    source_hashes = {
        "a/x": compute_source_hash([("a/x/a.py#A", "sk1")]),
        "b/y": compute_source_hash([("b/y/b.py#B", "sk2")]),
    }
    c = MagicMock()
    c.store.list_base_members.return_value = raw
    c.graph = None
    c.summary_store.get_source_hashes.return_value = source_hashes
    c.summary_store.get_completed_depth.return_value = completed_depth
    c.summary_store.get_completed_layout.return_value = (
        compute_layout_token(completed_depth, {})
        if completed_depth is not None
        else None
    )
    c.summary_store.get_updated_ats.return_value = {}
    c.summary_store.get_fragments.return_value = (
        [
            {
                "cluster_key": cluster_key,
                "path": path,
                "fingerprint": fingerprints[path],
                "summary": title,
                "provenance": {
                    "_reviewer": {
                        "generation": "summary-fragment-v1",
                        "layout_token": compute_layout_token(fragment_depth, {}),
                        "depth": fragment_depth,
                    }
                },
            }
            for cluster_key, path, title in (
                ("a/x", "a/x/a.py", "A"),
                ("b/y", "b/y/b.py", "B"),
            )
        ]
        if fragment_depth is not None
        else []
    )
    c.summary_store.commit_summary_bundle.return_value = {
        "created": 1,
        "reused": 0,
        "removed": 0,
        "moved": 0,
    }
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    c.summary_store.set_embedding_if_source_hash.return_value = True
    return c, source_hashes, fingerprints


def _persist_single_pending_fragment(
    svc: MCPReviewService,
    components,
    cluster: dict,
) -> dict:
    work = svc.get_subsystem_summary_work(
        "o/n",
        "dev",
        cluster["cluster_key"],
        cluster["source_hash"],
    )
    pending = work["added_files"] + work["changed_files"]
    [file_work] = pending
    result = svc.index_subsystem_summary(
        "o/n",
        "dev",
        cluster["cluster_key"],
        cluster["cluster_key"],
        "Сводка",
        cluster["source_hash"],
        fragments=[
            {
                **file_work,
                "summary": f"Фрагмент {file_work['path']}",
                "provenance": {"model": "cheap"},
            }
        ],
    )
    assert result["stored"] is True
    [stored] = components.summary_store.commit_summary_bundle.call_args.kwargs[
        "new_fragments"
    ]
    return {"cluster_key": cluster["cluster_key"], **stored}


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
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []
    svc = _svc(c)
    [cl] = svc.list_subsystem_clusters("o/n", "dev")["clusters"]
    assert cl["stale"] is False


def test_list_subsystem_clusters_adds_file_delta_without_changing_old_fields():
    from reviewer.graph.summaries import (
        Member,
        compute_file_fingerprints,
        compute_layout_token,
        compute_source_hash,
    )

    c = MagicMock()
    raw = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    c.store.list_base_members.return_value = raw
    c.graph = None
    [member] = [
        Member(
            node_id="reviewer/index/a.py#A",
            path="reviewer/index/a.py",
            content_hash="h1",
            start_line=1,
            skeleton_hash="sk1",
        )
    ]
    file_hash = compute_file_fingerprints([member])["reviewer/index/a.py"]
    source_hash = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": source_hash}
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_completed_layout.return_value = compute_layout_token(2, {})
    c.summary_store.get_fragments.return_value = [
        {
            "cluster_key": "reviewer/index",
            "path": "reviewer/index/a.py",
            "fingerprint": file_hash,
            "summary": "A",
            "provenance": {},
        }
    ]

    out = _svc(c).list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)

    [cluster] = out["clusters"]
    assert {
        key: cluster[key]
        for key in (
            "cluster_key",
            "num_members",
            "files",
            "top_symbols",
            "source_hash",
            "stale",
        )
    } == {
        "cluster_key": "reviewer/index",
        "num_members": 1,
        "files": ["reviewer/index/a.py"],
        "top_symbols": [
            {"node_id": "reviewer/index/a.py#A", "file": "reviewer/index/a.py", "line": 1}
        ],
        "source_hash": source_hash,
        "stale": False,
    }
    assert cluster["added_files"] == []
    assert cluster["changed_files"] == []
    assert cluster["removed_files"] == []
    assert cluster["moved_files"] == []
    assert cluster["reused_files"] == 1
    assert cluster["bootstrap"] is False
    assert cluster["full_rebuild"] is False
    assert out["deferred_files"] == 0


def test_list_subsystem_clusters_counts_pending_files_in_deferred_clusters():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a/x/a.py", "A", "h1", 1, "sk1"),
        ("b/y/b.py", "B", "h2", 1, "sk2"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []

    out = _svc(c).list_subsystem_clusters("o/n", "dev", depth=2, min_size=1, cap=1)

    assert [cluster["cluster_key"] for cluster in out["clusters"]] == ["a/x"]
    assert out["deferred"] == 1
    assert out["deferred_files"] == 1


def test_capped_bootstrap_converges_without_regenerating_completed_cluster():
    c, _source_hashes, _fingerprints = _two_cluster_generation_state(
        completed_depth=None,
        fragment_depth=None,
    )
    svc = _svc(c)

    first = svc.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=1
    )

    [first_selected] = [
        cluster
        for cluster in first["clusters"]
        if cluster["stale"] or cluster["bootstrap"]
    ]
    assert first_selected["cluster_key"] == "a/x"
    assert first_selected["stale"] is False
    assert first_selected["bootstrap"] is True
    assert first["deferred"] == 1
    assert first["deferred_files"] == 1

    first_stored = _persist_single_pending_fragment(svc, c, first_selected)
    c.summary_store.get_fragments.return_value = [first_stored]
    c.summary_store.commit_summary_bundle.reset_mock()

    second = svc.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=1
    )

    assert second["deferred"] == 0
    [second_selected] = [
        cluster
        for cluster in second["clusters"]
        if cluster["stale"] or cluster["bootstrap"]
    ]
    assert second_selected["cluster_key"] == "b/y"
    completed = next(
        cluster for cluster in second["clusters"]
        if cluster["cluster_key"] == "a/x"
    )
    assert completed["stale"] is False
    assert completed["bootstrap"] is False
    assert completed["added_files"] == []
    assert completed["changed_files"] == []
    assert completed["reused_files"] == 1
    completed_work = svc.get_subsystem_summary_work(
        "o/n", "dev", "a/x", completed["source_hash"]
    )
    assert completed_work["bootstrap"] is False
    assert completed_work["added_files"] == []
    assert completed_work["changed_files"] == []
    assert [item["path"] for item in completed_work["reused_fragments"]] == [
        "a/x/a.py"
    ]

    second_stored = _persist_single_pending_fragment(svc, c, second_selected)
    c.summary_store.get_fragments.return_value = [first_stored, second_stored]

    final = svc.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=1
    )
    assert final["deferred"] == 0
    assert not [
        cluster
        for cluster in final["clusters"]
        if cluster["stale"] or cluster["bootstrap"]
    ]


def test_capped_depth_rebuild_converges_without_regenerating_completed_cluster():
    c, _source_hashes, _fingerprints = _two_cluster_generation_state(
        completed_depth=1,
        fragment_depth=1,
    )
    svc = _svc(c)

    first = svc.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=1
    )

    [first_selected] = [
        cluster for cluster in first["clusters"] if cluster["stale"]
    ]
    assert first_selected["cluster_key"] == "a/x"
    assert first_selected["full_rebuild"] is True
    assert first["deferred"] == 1

    first_stored = _persist_single_pending_fragment(svc, c, first_selected)
    old_second = next(
        fragment
        for fragment in c.summary_store.get_fragments.return_value
        if fragment["cluster_key"] == "b/y"
    )
    c.summary_store.get_fragments.return_value = [first_stored, old_second]
    c.summary_store.commit_summary_bundle.reset_mock()

    second = svc.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=1
    )

    assert second["deferred"] == 0
    [second_selected] = [
        cluster for cluster in second["clusters"] if cluster["stale"]
    ]
    assert second_selected["cluster_key"] == "b/y"
    completed = next(
        cluster for cluster in second["clusters"]
        if cluster["cluster_key"] == "a/x"
    )
    assert completed["stale"] is False
    assert completed["full_rebuild"] is False
    assert completed["changed_files"] == []
    assert completed["reused_files"] == 1
    completed_work = svc.get_subsystem_summary_work(
        "o/n", "dev", "a/x", completed["source_hash"]
    )
    assert completed_work["full_rebuild"] is False
    assert completed_work["added_files"] == []
    assert completed_work["changed_files"] == []

    second_stored = _persist_single_pending_fragment(svc, c, second_selected)
    c.summary_store.get_fragments.return_value = [first_stored, second_stored]

    final = svc.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=1
    )
    assert final["deferred"] == 0
    assert not [cluster for cluster in final["clusters"] if cluster["stale"]]


def test_capped_override_layout_rebuild_converges_at_fixed_default_depth():
    from reviewer.graph.summaries import compute_layout_token

    c, _source_hashes, _fingerprints = _two_cluster_generation_state(
        completed_depth=2,
        fragment_depth=2,
    )
    svc = _svc(c)
    svc._resolve_summary_depth = lambda repo, branch: (
        2,
        {"a/x": 3},
        ".review.yml",
    )
    old_layout = compute_layout_token(2, {})
    new_layout = compute_layout_token(2, {"a/x": 3})
    assert old_layout != new_layout

    first = svc.list_subsystem_clusters(
        "o/n", "dev", depth=None, min_size=1, cap=1
    )

    assert first["layout_token"] == new_layout
    [first_selected] = [
        cluster for cluster in first["clusters"] if cluster["full_rebuild"]
    ]
    assert first_selected["cluster_key"] == "a/x"
    assert first["deferred"] == 1

    first_stored = _persist_single_pending_fragment(svc, c, first_selected)
    old_second = next(
        fragment
        for fragment in c.summary_store.get_fragments.return_value
        if fragment["cluster_key"] == "b/y"
    )
    c.summary_store.get_fragments.return_value = [first_stored, old_second]
    c.summary_store.commit_summary_bundle.reset_mock()

    second = svc.list_subsystem_clusters(
        "o/n", "dev", depth=None, min_size=1, cap=1
    )

    completed = next(
        cluster for cluster in second["clusters"]
        if cluster["cluster_key"] == "a/x"
    )
    [second_selected] = [
        cluster for cluster in second["clusters"] if cluster["full_rebuild"]
    ]
    assert completed["full_rebuild"] is False
    assert completed["reused_files"] == 1
    assert second_selected["cluster_key"] == "b/y"
    assert second["deferred"] == 0

    second_stored = _persist_single_pending_fragment(svc, c, second_selected)
    c.summary_store.get_fragments.return_value = [first_stored, second_stored]

    final = svc.list_subsystem_clusters(
        "o/n", "dev", depth=None, min_size=1, cap=1
    )
    assert final["deferred"] == 0
    assert not [
        cluster for cluster in final["clusters"]
        if cluster["full_rebuild"]
    ]


def test_list_subsystem_clusters_treats_same_key_depth_rebuild_as_stale_and_deferred():
    from datetime import datetime

    from reviewer.graph.summaries import compute_layout_token, compute_source_hash

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a.py", "A", "h1", 1, "sk1"),
        ("pkg/deep/b.py", "B", "h2", 1, "sk2"),
    ]
    c.graph = None
    root_hash = compute_source_hash([("a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {"<root>": root_hash}
    c.summary_store.get_completed_depth.return_value = 3
    c.summary_store.get_completed_layout.return_value = compute_layout_token(3, {})
    c.summary_store.get_fragments.return_value = []

    uncapped = _svc(c).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0
    )

    root = next(
        cluster for cluster in uncapped["clusters"]
        if cluster["cluster_key"] == "<root>"
    )
    assert root["source_hash"] == root_hash
    assert root["full_rebuild"] is True
    assert root["stale"] is True
    assert root["added_files"] == []
    assert [item["path"] for item in root["changed_files"]] == ["a.py"]

    c.summary_store.get_updated_ats.return_value = {
        "<root>": datetime(2026, 1, 1)
    }
    capped = _svc(c).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=1
    )

    assert [cluster["cluster_key"] for cluster in capped["clusters"]] == [
        "pkg/deep"
    ]
    assert capped["deferred"] == 1
    assert capped["deferred_files"] == 1


def test_get_subsystem_summary_work_returns_delta_and_reusable_fragment_texts():
    from reviewer.graph.summaries import (
        Member,
        compute_file_fingerprints,
        compute_layout_token,
        compute_source_hash,
    )

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 2, "sk2"),
    ]
    c.graph = None
    members = [
        Member("reviewer/index/a.py#A", "reviewer/index/a.py", "h1", "sk1", 1),
        Member("reviewer/index/b.py#B", "reviewer/index/b.py", "h2", "sk2", 2),
    ]
    fingerprints = compute_file_fingerprints(members)
    source_hash = compute_source_hash(
        [("reviewer/index/a.py#A", "sk1"), ("reviewer/index/b.py#B", "sk2")]
    )
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_completed_layout.return_value = compute_layout_token(2, {})
    c.summary_store.get_fragments.return_value = [
        {
            "cluster_key": "reviewer/index",
            "path": "reviewer/index/a.py",
            "fingerprint": fingerprints["reviewer/index/a.py"],
            "summary": "Фрагмент A",
            "provenance": {"generator": "test"},
        }
    ]

    out = _svc(c).get_subsystem_summary_work(
        "o/n", "dev", "reviewer/index", source_hash
    )

    assert out["ready"] is True
    assert out["added_files"] == [
        {
            "path": "reviewer/index/b.py",
            "fingerprint": fingerprints["reviewer/index/b.py"],
        }
    ]
    assert out["changed_files"] == []
    assert out["removed_files"] == []
    assert out["moved_files"] == []
    assert out["reused_fragments"] == [
        {
            "path": "reviewer/index/a.py",
            "fingerprint": fingerprints["reviewer/index/a.py"],
            "summary": "Фрагмент A",
            "provenance": {"generator": "test"},
        }
    ]
    assert out["bootstrap"] is False
    assert out["full_rebuild"] is False


def test_get_subsystem_summary_work_rejects_stale_hash_without_generation_data():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]
    c.graph = None
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []

    out = _svc(c).get_subsystem_summary_work(
        "o/n", "dev", "reviewer/index", "STALE"
    )

    assert out["ready"] is False
    assert "note" in out
    assert "added_files" not in out
    assert "reused_fragments" not in out


def test_get_subsystem_summary_work_bootstraps_every_current_path_without_depth_state():
    from reviewer.graph.summaries import compute_source_hash

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 2, "sk2"),
    ]
    c.graph = None
    c.summary_store.get_completed_depth.return_value = None
    c.summary_store.get_fragments.return_value = []
    source_hash = compute_source_hash(
        [("reviewer/index/a.py#A", "sk1"), ("reviewer/index/b.py#B", "sk2")]
    )

    out = _svc(c).get_subsystem_summary_work(
        "o/n", "dev", "reviewer/index", source_hash
    )

    assert out["bootstrap"] is True
    assert out["full_rebuild"] is False
    assert [item["path"] for item in out["added_files"]] == [
        "reviewer/index/a.py",
        "reviewer/index/b.py",
    ]
    assert out["changed_files"] == []
    assert out["reused_fragments"] == []


def test_get_subsystem_summary_work_rebuilds_all_files_when_depth_changed():
    from reviewer.graph.summaries import (
        Member,
        compute_file_fingerprints,
        compute_layout_token,
        compute_source_hash,
    )

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 2, "sk2"),
    ]
    c.graph = None
    members = [
        Member("reviewer/index/a.py#A", "reviewer/index/a.py", "h1", "sk1", 1),
        Member("reviewer/index/b.py#B", "reviewer/index/b.py", "h2", "sk2", 2),
    ]
    fingerprints = compute_file_fingerprints(members)
    c.summary_store.get_completed_depth.return_value = 3
    c.summary_store.get_completed_layout.return_value = compute_layout_token(3, {})
    c.summary_store.get_fragments.return_value = [
        {
            "cluster_key": "reviewer/index",
            "path": "reviewer/index/a.py",
            "fingerprint": fingerprints["reviewer/index/a.py"],
            "summary": "A",
            "provenance": {},
        },
        {
            "cluster_key": "reviewer",
            "path": "reviewer/index/b.py",
            "fingerprint": fingerprints["reviewer/index/b.py"],
            "summary": "B",
            "provenance": {},
        },
    ]
    source_hash = compute_source_hash(
        [("reviewer/index/a.py#A", "sk1"), ("reviewer/index/b.py#B", "sk2")]
    )

    out = _svc(c).get_subsystem_summary_work(
        "o/n", "dev", "reviewer/index", source_hash
    )

    assert out["bootstrap"] is False
    assert out["full_rebuild"] is True
    assert out["added_files"] == []
    assert [item["path"] for item in out["changed_files"]] == [
        "reviewer/index/a.py",
        "reviewer/index/b.py",
    ]
    assert out["moved_files"] == []
    assert out["reused_fragments"] == []


def test_list_subsystem_clusters_empty_index_returns_note():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    svc = _svc(c)
    out = svc.list_subsystem_clusters("o/n", "dev")
    assert out["clusters"] == []
    assert "note" in out
    assert "branch" in out
    assert out["deferred"] == 0
    c.summary_store.get_fragments.assert_not_called()
    c.summary_store.get_completed_depth.assert_not_called()


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
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    c.summary_store.set_embedding_if_source_hash.return_value = True
    svc = _svc(c)

    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", sh)
    assert out == {
        "cluster_key": "reviewer/index",
        "stored": True,
        "members": 2,
        "embedded": True,
    }
    # upsert получил выведенный (отсортированный) member_node_ids, а не []
    args = c.summary_store.upsert_summary.call_args.args
    assert args[5] == ["reviewer/index/a.py#A", "reviewer/index/b.py#B"]

    got = svc.get_subsystem_summaries("o/n", "dev")
    assert got["summaries"][0]["cluster_key"] == "reviewer/index"


def test_index_subsystem_summary_rejects_fragment_bundle_when_source_hash_raced():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]
    c.graph = None
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []

    out = _svc(c).index_subsystem_summary(
        "o/n",
        "dev",
        "reviewer/index",
        "Индекс",
        "...",
        "STALE",
        fragments=[],
    )

    assert out["stored"] is False
    assert out["race"] is True
    c.summary_store.commit_summary_bundle.assert_not_called()
    c.summary_store.upsert_summary.assert_not_called()
    c.embedder.embed_documents.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [
            {
                "path": "reviewer/index/a.py",
                "fingerprint": "wrong",
                "summary": "A",
                "provenance": {},
            }
        ],
        [
            {
                "path": "reviewer/index/a.py",
                "fingerprint": "unused",
                "summary": "A",
                "provenance": {},
            },
            {
                "path": "reviewer/index/extra.py",
                "fingerprint": "extra",
                "summary": "Extra",
                "provenance": {},
            },
        ],
        [
            {
                "path": "reviewer/index/a.py",
                "fingerprint": "unused",
                "provenance": {},
            }
        ],
    ],
    ids=["missing", "wrong-fingerprint", "extra-path", "incomplete"],
)
def test_index_subsystem_summary_rejects_invalid_fragment_coverage(payload):
    from reviewer.graph.summaries import compute_source_hash

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]
    c.graph = None
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []
    source_hash = compute_source_hash([("reviewer/index/a.py#A", "sk1")])

    out = _svc(c).index_subsystem_summary(
        "o/n",
        "dev",
        "reviewer/index",
        "Индекс",
        "...",
        source_hash,
        fragments=payload,
    )

    assert out["stored"] is False
    assert "note" in out
    c.summary_store.commit_summary_bundle.assert_not_called()
    c.summary_store.upsert_summary.assert_not_called()
    c.embedder.embed_documents.assert_not_called()


def test_index_subsystem_summary_commits_bundle_before_embedding_with_hash_cas():
    from reviewer.graph.summaries import (
        Member,
        compute_file_fingerprints,
        compute_layout_token,
        compute_source_hash,
    )

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]
    c.graph = None
    member = Member("reviewer/index/a.py#A", "reviewer/index/a.py", "h1", "sk1", 1)
    file_hash = compute_file_fingerprints([member])["reviewer/index/a.py"]
    source_hash = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []
    order = []
    c.summary_store.commit_summary_bundle.side_effect = lambda *args, **kwargs: (
        order.append("commit")
        or {"created": 1, "reused": 0, "removed": 0, "moved": 0}
    )
    c.embedder.embed_documents.side_effect = lambda texts: (
        order.append("embed") or [[0.5, 0.5]]
    )
    c.summary_store.set_embedding_if_source_hash.side_effect = (
        lambda *args, **kwargs: order.append("cas") or True
    )

    out = _svc(c).index_subsystem_summary(
        "o/n",
        "dev",
        "reviewer/index",
        "Индекс",
        "Тело",
        source_hash,
        fragments=[
            {
                "path": "reviewer/index/a.py",
                "fingerprint": file_hash,
                "summary": "A",
                "provenance": {
                    "generator": "test",
                    "_reviewer": {
                        "generation": "forged",
                        "depth": 999,
                    },
                },
            }
        ],
    )

    assert order == ["commit", "embed", "cas"]
    c.summary_store.commit_summary_bundle.assert_called_once_with(
        "o/n",
        "dev",
        "reviewer/index",
        "Индекс",
        "Тело",
        ["reviewer/index/a.py#A"],
        source_hash,
        current_fingerprints={"reviewer/index/a.py": file_hash},
        new_fragments=[
            {
                "path": "reviewer/index/a.py",
                "fingerprint": file_hash,
                "summary": "A",
                "provenance": {
                    "generator": "test",
                        "_reviewer": {
                            "generation": "summary-fragment-v1",
                            "layout_token": compute_layout_token(2, {}),
                            "depth": 2,
                        },
                },
            }
        ],
    )
    c.embedder.embed_documents.assert_called_once_with(["Индекс\nТело"])
    c.summary_store.set_embedding_if_source_hash.assert_called_once_with(
        "o/n",
        "dev",
        "reviewer/index",
        source_hash,
        [0.5, 0.5],
        title="Индекс",
        summary="Тело",
    )
    assert out == {
        "cluster_key": "reviewer/index",
        "stored": True,
        "members": 1,
        "created": 1,
        "reused": 0,
        "removed": 0,
        "moved": 0,
        "embedded": True,
    }


@pytest.mark.parametrize("cas_result", [False, RuntimeError("voyage down")])
def test_index_subsystem_summary_reports_backfill_when_embedding_is_not_saved(cas_result):
    from reviewer.graph.summaries import Member, compute_file_fingerprints, compute_source_hash

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]
    c.graph = None
    member = Member("reviewer/index/a.py#A", "reviewer/index/a.py", "h1", "sk1", 1)
    file_hash = compute_file_fingerprints([member])["reviewer/index/a.py"]
    source_hash = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []
    c.summary_store.commit_summary_bundle.return_value = {
        "created": 1,
        "reused": 0,
        "removed": 0,
        "moved": 0,
    }
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    if isinstance(cas_result, Exception):
        c.embedder.embed_documents.side_effect = cas_result
    else:
        c.summary_store.set_embedding_if_source_hash.return_value = cas_result

    out = _svc(c).index_subsystem_summary(
        "o/n",
        "dev",
        "reviewer/index",
        "Индекс",
        "Тело",
        source_hash,
        fragments=[
            {
                "path": "reviewer/index/a.py",
                "fingerprint": file_hash,
                "summary": "A",
                "provenance": {},
            }
        ],
    )

    assert out["stored"] is True
    assert out["embedded"] is False
    assert "бэкфилл" in out["note"]


def test_index_subsystem_summary_legacy_path_is_strict_hash_checked():
    from reviewer.graph.summaries import compute_source_hash

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]
    c.graph = None
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    c.summary_store.set_embedding_if_source_hash.return_value = True
    source_hash = compute_source_hash([("reviewer/index/a.py#A", "sk1")])

    out = _svc(c).index_subsystem_summary(
        "o/n", "dev", "reviewer/index", "Индекс", "Тело", source_hash
    )

    c.summary_store.upsert_summary.assert_called_once_with(
        "o/n",
        "dev",
        "reviewer/index",
        "Индекс",
        "Тело",
        ["reviewer/index/a.py#A"],
        source_hash,
        embedding=None,
        preserve_embedding=False,
    )
    c.summary_store.commit_summary_bundle.assert_not_called()
    assert out["stored"] is True
    assert out["members"] == 1
    assert out["embedded"] is True


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


def test_index_subsystem_summary_stale_hash_rejects_legacy_write():
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    svc = _svc(c)
    # передан неактуальный source_hash → пере-вычисленный не совпадёт
    out = svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "...", "STALE")
    assert out["stored"] is False
    assert out["race"] is True
    assert "note" in out
    c.summary_store.upsert_summary.assert_not_called()
    c.embedder.embed_documents.assert_not_called()


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


def _service_for_summary_resolution() -> tuple[MCPReviewService, MagicMock]:
    """Сервис с VCS-фабрикой для изолированного резолва policy сводок."""
    vcs = MagicMock()
    svc = MCPReviewService(_settings(), components=MagicMock(),
                           vcs_factory=lambda owner, name: vcs)
    return svc, vcs


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


def test_prune_subsystem_summaries_verifies_list_snapshot_before_pruning():
    from reviewer.graph.summaries import compute_file_fingerprints, compute_layout_token, Member

    c = MagicMock()
    raw = [
        ("a/x/f.py", "F", "h", 1, "skf"),
        ("b/y/g.py", "G", "h", 1, "skg"),
    ]
    c.store.list_base_members.return_value = raw
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    c.summary_store.get_fragments.return_value = []
    c.summary_store.prune_verified_layout.return_value = {
        "completed": True,
        "race": False,
        "deferred": 0,
        "pruned": 2,
        "fragments_pruned": 3,
        "depth": 2,
        "layout_token": compute_layout_token(2, {}),
    }
    svc = _svc(c)
    listed = svc.list_subsystem_clusters("o/n", "dev", cap=0)
    expected_hashes = {
        cluster["cluster_key"]: cluster["source_hash"]
        for cluster in listed["clusters"]
    }

    out = svc.prune_subsystem_summaries(
        "o/n",
        "dev",
        layout_token=listed["layout_token"],
        expected_source_hashes=expected_hashes,
    )

    assert out == {
        "completed": True,
        "race": False,
        "deferred": 0,
        "pruned": 2,
        "kept": 2,
        "fragments_pruned": 3,
        "depth": 2,
        "layout_token": compute_layout_token(2, {}),
    }
    members = [
        Member(f"{path}#{symbol}", path, content_hash, skeleton_hash, start_line)
        for path, symbol, content_hash, start_line, skeleton_hash in raw
    ]
    fingerprints = compute_file_fingerprints(members)
    c.summary_store.prune_verified_layout.assert_called_once_with(
        "o/n",
        "dev",
        expected_hashes,
        {
            "a/x": {"a/x/f.py": fingerprints["a/x/f.py"]},
            "b/y": {"b/y/g.py": fingerprints["b/y/g.py"]},
        },
        2,
        compute_layout_token(2, {}),
    )


def test_prune_subsystem_summaries_rejects_legacy_call_without_snapshot():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"),
    ]
    c.graph = None
    svc = _svc(c)

    out = svc.prune_subsystem_summaries("o/n", "dev")

    assert out["completed"] is False
    assert out["race"] is True
    assert out["deferred"] == 1
    assert out["pruned"] == 0
    assert "snapshot" in out["note"]
    c.summary_store.prune_verified_layout.assert_not_called()


def test_prune_subsystem_summaries_rejects_policy_change_after_list():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("a/x/f.py", "F", "h", 1, "skf"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    c.summary_store.get_fragments.return_value = []
    svc = _svc(c)
    listed = svc.list_subsystem_clusters("o/n", "dev", cap=0)
    expected_hashes = {
        cluster["cluster_key"]: cluster["source_hash"]
        for cluster in listed["clusters"]
    }
    svc._resolve_summary_depth = lambda repo, branch: (
        2,
        {"a/x": 3},
        ".review.yml",
    )

    out = svc.prune_subsystem_summaries(
        "o/n",
        "dev",
        layout_token=listed["layout_token"],
        expected_source_hashes=expected_hashes,
    )

    assert out["completed"] is False
    assert out["race"] is True
    assert out["deferred"] == 1
    assert out["pruned"] == 0
    assert "layout" in out["note"]
    c.summary_store.prune_verified_layout.assert_not_called()


def test_prune_subsystem_summaries_empty_base_is_noop():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    svc = _svc(c)
    out = svc.prune_subsystem_summaries(
        "o/n",
        "dev",
        layout_token="snapshot",
        expected_source_hashes={},
    )
    assert out["completed"] is False
    assert out["race"] is True
    assert out["deferred"] == 0
    assert out["pruned"] == 0
    assert out["kept"] == 0
    assert out["fragments_pruned"] == 0
    assert out["depth"] == 2
    assert "note" in out
    c.summary_store.prune_verified_layout.assert_not_called()  # base пуст → не вайпаем


def test_index_subsystem_summary_embeds_when_hash_changed():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {}          # сводки ещё нет → hash изменился
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    c.summary_store.set_embedding_if_source_hash.return_value = True
    svc = _svc(c)
    svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "тело", sh)
    c.embedder.embed_documents.assert_called_once_with(["Индекс\nтело"])
    assert c.summary_store.upsert_summary.call_args.kwargs["embedding"] is None
    c.summary_store.set_embedding_if_source_hash.assert_called_once_with(
        "o/n",
        "dev",
        "reviewer/index",
        sh,
        [0.5, 0.5],
        title="Индекс",
        summary="тело",
    )


def test_index_subsystem_summary_refreshes_embedding_after_legacy_commit():
    from reviewer.graph.summaries import compute_source_hash
    c = MagicMock()
    c.store.list_base_members.return_value = [("reviewer/index/a.py", "A", "h1", 1, "sk1")]
    sh = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": sh}   # хеш совпал
    c.embedder.embed_documents.return_value = [[0.5, 0.5]]
    c.summary_store.set_embedding_if_source_hash.return_value = True
    svc = _svc(c)
    svc.index_subsystem_summary("o/n", "dev", "reviewer/index", "Индекс", "тело", sh)
    c.embedder.embed_documents.assert_called_once_with(["Индекс\nтело"])
    assert c.summary_store.upsert_summary.call_args.kwargs["embedding"] is None


def test_resolve_summary_topk_threshold_override_from_review_yml():
    svc = _svc_with_vcs(_FakeVCS("summary_topk_threshold: 5"))
    assert svc._resolve_summary_topk_threshold("o/n", "dev") == (5, ".review.yml")


def test_resolve_summary_topk_threshold_no_key_falls_back_to_env():
    svc = _svc_with_vcs(_FakeVCS("severity_threshold: high"))
    val, source = svc._resolve_summary_topk_threshold("o/n", "dev")
    assert val == svc.settings.summary_topk_threshold
    assert source == "env"


def test_summary_threshold_reports_home_repo_source(isolated_xdg_config_home):
    """Источник порога указывает репозиторный home-слой, а не env."""
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("summary_topk_threshold: 7\n", encoding="utf-8")
    svc, vcs = _service_for_summary_resolution()
    vcs.get_file_at_ref.return_value = None

    value, source = svc._resolve_summary_topk_threshold("o/r", "main")

    assert value == 7
    assert source == "home:repos/o/r.yml"


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


def test_current_subsystem_hashes_rejects_conflicting_raw_layout_aliases():
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("x/y/z.py", "Z", "h1", 1, "sk1"),
    ]
    c.graph = None
    svc = _svc(c)
    svc._resolve_summary_depth = lambda repo, branch: (
        2,
        {"/x/": 1, "x": 2},
        ".review.yml",
    )

    assert svc._current_subsystem_hashes("o/n", "dev") is None


def test_backfill_summary_embeddings_fills_pending():
    c = MagicMock()
    c.summary_store.get_pending_embeddings.return_value = [
        {
            "cluster_key": "auth",
            "title": "Авторизация",
            "summary": "тело",
            "source_hash": "auth-hash",
        }
    ]
    c.embedder.embed_documents.return_value = [[0.3, 0.4]]
    c.summary_store.set_embedding_if_source_hash.return_value = True
    svc = _svc(c)
    out = svc.backfill_summary_embeddings("o/n", "dev")
    assert out == {"embedded": 1}
    c.embedder.embed_documents.assert_called_once_with(["Авторизация\nтело"])
    c.summary_store.set_embedding_if_source_hash.assert_called_once_with(
        "o/n",
        "dev",
        "auth",
        "auth-hash",
        [0.3, 0.4],
        title="Авторизация",
        summary="тело",
    )


def test_backfill_summary_embeddings_counts_only_successful_exact_cas():
    c = MagicMock()
    c.summary_store.get_pending_embeddings.return_value = [
        {
            "cluster_key": "auth",
            "title": "Авторизация",
            "summary": "старый текст",
            "source_hash": "same-hash",
        },
        {
            "cluster_key": "index",
            "title": "Индекс",
            "summary": "текущий текст",
            "source_hash": "index-hash",
        },
    ]
    c.embedder.embed_documents.return_value = [[0.1, 0.2], [0.3, 0.4]]
    c.summary_store.set_embedding_if_source_hash.side_effect = [False, True]

    out = _svc(c).backfill_summary_embeddings("o/n", "dev")

    assert out == {"embedded": 1}
    assert c.summary_store.set_embedding_if_source_hash.call_count == 2


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
        {
            "cluster_key": "auth",
            "title": "Авторизация",
            "summary": "тело",
            "source_hash": "auth-hash",
        }
    ]
    c.embedder.embed_documents.side_effect = RuntimeError("voyage down")
    svc = _svc(c)
    out = svc.backfill_summary_embeddings("o/n", "dev")
    assert out["embedded"] == 0
    assert "note" in out
    c.summary_store.set_embedding_if_source_hash.assert_not_called()


def _one_cluster_components():
    """Фейк с одним кластером reviewer/index из двух файлов: один свежий (fragment
    сохранён), второй новый → delta.added непустая, delta.reused непустая."""
    from reviewer.graph.summaries import (
        Member,
        compute_file_fingerprints,
        compute_layout_token,
        compute_source_hash,
    )

    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1"),
        ("reviewer/index/b.py", "B", "h2", 1, "sk2"),
    ]
    c.graph = None
    members = [
        Member("reviewer/index/a.py#A", "reviewer/index/a.py", "h1", "sk1", 1),
        Member("reviewer/index/b.py#B", "reviewer/index/b.py", "h2", "sk2", 1),
    ]
    fingerprints = compute_file_fingerprints(members)
    source_hash = compute_source_hash(
        [("reviewer/index/a.py#A", "sk1"), ("reviewer/index/b.py#B", "sk2")]
    )
    c.summary_store.get_source_hashes.return_value = {"reviewer/index": source_hash}
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_completed_layout.return_value = compute_layout_token(2, {})
    c.summary_store.get_fragments.return_value = [
        {
            "cluster_key": "reviewer/index",
            "path": "reviewer/index/a.py",
            "fingerprint": fingerprints["reviewer/index/a.py"],
            "summary": "A",
            "provenance": {},
        }
    ]
    return c


def test_compact_cluster_record_has_exact_keys():
    out = _svc(_one_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    [cluster] = out["clusters"]
    assert set(cluster) == {
        "cluster_key",
        "num_members",
        "source_hash",
        "stale",
        "bootstrap",
        "full_rebuild",
        "reused_files",
        "added",
        "changed",
        "removed",
        "moved",
    }
    assert cluster["cluster_key"] == "reviewer/index"
    assert cluster["num_members"] == 2
    assert cluster["added"] == 1
    assert cluster["reused_files"] == 1


def test_compact_response_carries_no_paths_or_fingerprints():
    """Сжатая запись не должна содержать ни путей, ни 64-символьных hex-хешей,
    кроме разрешённых cluster_key и source_hash."""
    import re

    out = _svc(_one_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    [cluster] = out["clusters"]
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    for key, value in cluster.items():
        if key in ("cluster_key", "source_hash"):
            continue
        assert not isinstance(value, (list, dict)), f"{key} остался структурой"
        if isinstance(value, str):
            assert "/" not in value, f"{key} похоже на путь: {value}"
            assert not hex64.match(value), f"{key} похоже на fingerprint"


def test_compact_counters_match_full_format_list_lengths():
    svc_full = _svc(_one_cluster_components())
    svc_compact = _svc(_one_cluster_components())
    full = svc_full.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    compact = svc_compact.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    by_key_full = {c["cluster_key"]: c for c in full["clusters"]}
    by_key_compact = {c["cluster_key"]: c for c in compact["clusters"]}
    assert by_key_full.keys() == by_key_compact.keys()
    for key, cf in by_key_full.items():
        cc = by_key_compact[key]
        assert cc["added"] == len(cf["added_files"])
        assert cc["changed"] == len(cf["changed_files"])
        assert cc["removed"] == len(cf["removed_files"])
        assert cc["moved"] == len(cf["moved_files"])
        assert cc["reused_files"] == cf["reused_files"]
        assert cc["num_members"] == cf["num_members"]
        assert cc["source_hash"] == cf["source_hash"]
        assert cc["stale"] == cf["stale"]
        assert cc["bootstrap"] == cf["bootstrap"]
        assert cc["full_rebuild"] == cf["full_rebuild"]


def test_full_format_is_default_and_top_level_fields_match_both_modes():
    svc_full = _svc(_one_cluster_components())
    svc_compact = _svc(_one_cluster_components())
    full = svc_full.list_subsystem_clusters("o/n", "dev", depth=2, min_size=1)
    compact = svc_compact.list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, compact=True
    )
    assert "files" in full["clusters"][0]           # дефолт — полный формат
    for field in ("branch", "depth", "layout_token", "depth_source",
                  "deferred", "deferred_files", "orphans"):
        assert full[field] == compact[field], field


def _four_cluster_components():
    """Фейк с четырьмя кластерами (d/x, b/x, a/x, c/x) — порядок членов намеренно
    не отсортирован, чтобы поймать зависимость выдачи от порядка обхода."""
    c = MagicMock()
    c.store.list_base_members.return_value = [
        ("d/x/d.py", "D", "h4", 1, "sk4"),
        ("b/x/b.py", "B", "h2", 1, "sk2"),
        ("a/x/a.py", "A", "h1", 1, "sk1"),
        ("c/x/c.py", "C", "h3", 1, "sk3"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    c.summary_store.get_completed_depth.return_value = 2
    c.summary_store.get_fragments.return_value = []
    return c


def test_pagination_full_walk_equals_unpaginated_call():
    unpaged = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True
    )
    expected = [c["cluster_key"] for c in unpaged["clusters"]]

    walked, offset, pages = [], 0, 0
    while True:
        page = _svc(_four_cluster_components()).list_subsystem_clusters(
            "o/n", "dev", depth=2, min_size=1, cap=0, compact=True,
            offset=offset, limit=2,
        )
        walked.extend(c["cluster_key"] for c in page["clusters"])
        pages += 1
        if not page["has_more"]:
            break
        offset += 2
        assert pages < 10, "пагинация не сходится"

    assert walked == expected
    assert len(walked) == len(set(walked)), "дубли при обходе страницами"
    assert unpaged["total_clusters"] == len(expected)


def test_pagination_order_is_reproducible_and_sorted_by_cluster_key():
    first = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True
    )
    second = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True
    )
    keys = [c["cluster_key"] for c in first["clusters"]]
    assert keys == [c["cluster_key"] for c in second["clusters"]]
    assert keys == sorted(keys)


def test_pagination_offset_beyond_set_returns_empty_page():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True,
        offset=99, limit=2,
    )
    assert out["clusters"] == []
    assert out["has_more"] is False
    assert out["total_clusters"] == 4


def test_pagination_limit_larger_than_set_returns_single_page():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True, limit=100
    )
    assert len(out["clusters"]) == 4
    assert out["has_more"] is False


def test_pagination_normalizes_negative_offset_and_nonpositive_limit():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, compact=True,
        offset=-5, limit=0,
    )
    assert out["offset"] == 0
    assert out["limit"] is None
    assert len(out["clusters"]) == 4


def test_global_fields_identical_on_every_page():
    """deferred / deferred_files / orphans / layout_token / total_clusters
    считаются по полному множеству и не зависят от страницы."""
    globals_fields = ("deferred", "deferred_files", "orphans",
                      "layout_token", "depth", "depth_source", "total_clusters")
    unpaged = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=2, compact=True
    )
    for offset in (0, 1, 2):
        page = _svc(_four_cluster_components()).list_subsystem_clusters(
            "o/n", "dev", depth=2, min_size=1, cap=2, compact=True,
            offset=offset, limit=1,
        )
        for field in globals_fields:
            assert page[field] == unpaged[field], f"{field} на offset={offset}"


def test_pagination_works_in_full_format_too():
    out = _svc(_four_cluster_components()).list_subsystem_clusters(
        "o/n", "dev", depth=2, min_size=1, cap=0, offset=1, limit=2
    )
    assert [c["cluster_key"] for c in out["clusters"]] == ["b/x", "c/x"]
    assert out["has_more"] is True
    assert "files" in out["clusters"][0]


def test_empty_index_note_carries_pagination_fields():
    c = MagicMock()
    c.store.list_base_members.return_value = []
    c.graph = None
    out = _svc(c).list_subsystem_clusters("o/n", "dev")
    assert out["clusters"] == []
    assert out["total_clusters"] == 0
    assert out["has_more"] is False
    assert "note" in out
