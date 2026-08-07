"""Тест per-path depth overrides в list/index/prune сводок (PRI-161, Task 6)."""
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.graph.summaries import compute_layout_token
from reviewer.mcp.service import MCPReviewService


def _svc(review_yml: str):
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    # review_branches включает "main" по умолчанию
    c = MagicMock()
    # base-состав: один файл в reviewer/index/sub, один в reviewer/mcp
    c.store.list_base_members.return_value = [
        ("reviewer/index/sub/x.py", "A", "h1", 1, "sk1"),
        ("reviewer/mcp/service.py", "B", "h2", 1, "sk2"),
    ]
    c.graph = None
    c.summary_store.get_source_hashes.return_value = {}
    c.summary_store.get_updated_ats.return_value = {}
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = review_yml
    svc = MCPReviewService(s, c, vcs_factory=lambda o, n: vcs)
    return svc


def test_list_clusters_honors_depth_override():
    svc = _svc("summary_cluster_depth: 2\n"
               "summary_cluster_depth_overrides:\n  reviewer/index: 3\n")
    out = svc.list_subsystem_clusters("o/n", "main", cap=0)
    keys = {c["cluster_key"] for c in out["clusters"]}
    assert "reviewer/index/sub" in keys     # override depth 3 → три сегмента
    assert "reviewer/mcp" in keys           # дефолт depth 2 → два сегмента


def test_changed_override_forces_rebuild_when_default_depth_is_unchanged():
    svc = _svc(
        "summary_cluster_depth: 2\n"
        "summary_cluster_depth_overrides:\n  reviewer/index: 3\n"
    )
    svc.components.summary_store.get_completed_depth.return_value = 2
    svc.components.summary_store.get_completed_layout.return_value = (
        compute_layout_token(2, {})
    )

    out = svc.list_subsystem_clusters("o/n", "main", cap=0)

    assert out["depth"] == 2
    assert out["layout_token"] == compute_layout_token(
        2,
        {"reviewer/index": 3},
    )
    assert all(cluster["full_rebuild"] for cluster in out["clusters"])
