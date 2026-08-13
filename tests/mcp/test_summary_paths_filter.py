"""PRI-245: фильтр кластеризации сводок применяется до build_clusters."""
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.graph.summaries import Member, build_clusters
from reviewer.index.pathfilter import is_ignored
from reviewer.mcp.service import MCPReviewService


def _members():
    return [
        Member("reviewer/index/store.py#A", "reviewer/index/store.py", "h1", "s1", 1),
        Member("tests/mcp/test_x.py#B", "tests/mcp/test_x.py", "h2", "s2", 1),
        Member("tests/skills/test_y.py#C", "tests/skills/test_y.py", "h3", "s3", 1),
    ]


def filter_members(members, ignore):
    """Ровно та фильтрация, которую применяет сервис (ссылочная реализация теста)."""
    return [m for m in members if not is_ignored(m.path, ignore)]


def test_test_trees_form_no_clusters_under_default_filter():
    kept = filter_members(_members(), ["tests", "test"])
    keys = {c.key for c in build_clusters(kept, None, depth=2)}
    assert keys == {"reviewer/index"}


def test_empty_filter_keeps_test_clusters():
    kept = filter_members(_members(), [])
    keys = {c.key for c in build_clusters(kept, None, depth=2)}
    assert "tests/mcp" in keys and "tests/skills" in keys


def test_filter_does_not_match_similarly_named_production_paths():
    members = [
        Member("reviewer/testing.py#A", "reviewer/testing.py", "h", "s", 1),
        Member("reviewer/test_utils.py#B", "reviewer/test_utils.py", "h", "s", 1),
    ]
    assert filter_members(members, ["tests", "test"]) == members


def _service(raw_members):
    settings = Settings()
    settings.voyage_api_key = "test"
    settings.github_token = "test"
    components = MagicMock()
    components.store.list_base_members.return_value = raw_members
    components.summary_store.get_fragments.return_value = []
    components.summary_store.get_completed_depth.return_value = None
    components.summary_store.get_completed_layout.return_value = None
    components.graph = None
    return MCPReviewService(settings, components)


def test_resolve_summary_layout_normalizes_ignore(monkeypatch):
    """Minor 3: `_resolve_summary_layout` отдаёт УЖЕ канонизированный ignore.

    Сырое значение с ведущим слэшем ("/tests") нормализуется в "tests" тем же
    способом, что и внутри `canonicalize_layout` — иначе `_summary_state`
    (нормализованный список) и `_current_subsystem_hashes` (сырой список)
    видят разные паттерны и расходятся в составе кластеров.
    """
    from reviewer.policy.policy import ReviewPolicy

    class _Meta:
        sources = {}

    service = _service([])
    policy = ReviewPolicy(summary_paths_ignore=["/tests", "tests/", " test "])
    monkeypatch.setattr(
        service, "_resolve_policy", lambda repo, branch: (policy, _Meta())
    )
    _, _, ignore, _ = service._resolve_summary_layout("owner/name", "dev")
    assert ignore == ["test", "tests"]


def test_leading_slash_ignore_yields_same_members_in_both_cluster_paths(monkeypatch):
    """Minor 3: раньше `_summary_state` (нормализованный ignore через
    `canonicalize_layout`) и `_current_subsystem_hashes` (сырой ignore из
    `_resolve_summary_layout`) видели разные наборы members для "/tests" —
    `is_ignored` не переваривает ведущий слэш, а `canonicalize_layout` его
    срезает. Итог был расхождением source_hash и вечным `stale=true`.
    """
    from reviewer.policy.policy import ReviewPolicy

    class _Meta:
        sources = {}

    raw = [
        ("reviewer/index/store.py", "A", "h1", 1, "s1"),
        ("tests/mcp/test_x.py", "B", "h2", 1, "s2"),
    ]
    service = _service(raw)
    policy = ReviewPolicy(summary_paths_ignore=["/tests"])
    monkeypatch.setattr(
        service, "_resolve_policy", lambda repo, branch: (policy, _Meta())
    )
    state = service._summary_state("owner/name", "dev")
    hashes = service._current_subsystem_hashes("owner/name", "dev")
    assert {c.key for c in state.clusters} == {"reviewer/index"}
    assert set(hashes) == {"reviewer/index"}
    assert hashes["reviewer/index"] == next(
        c.source_hash for c in state.clusters if c.key == "reviewer/index"
    )


def test_service_filters_members_in_both_cluster_paths(monkeypatch):
    """_summary_state и _current_subsystem_hashes видят ОДИН набор кластеров.

    Расхождение сделало бы каждую сводку вечно stale: source_hash из
    _summary_state не совпал бы с эталоном из _current_subsystem_hashes.
    """
    raw = [
        ("reviewer/index/store.py", "A", "h1", 1, "s1"),
        ("tests/mcp/test_x.py", "B", "h2", 1, "s2"),
    ]
    service = _service(raw)
    monkeypatch.setattr(
        service, "_resolve_summary_layout",
        lambda repo, branch: (2, {}, ["tests"], ".review.yml"),
    )
    state = service._summary_state("owner/name", "dev")
    hashes = service._current_subsystem_hashes("owner/name", "dev")
    assert {c.key for c in state.clusters} == {"reviewer/index"}
    assert set(hashes) == {"reviewer/index"}
    assert hashes["reviewer/index"] == next(
        c.source_hash for c in state.clusters if c.key == "reviewer/index"
    )
