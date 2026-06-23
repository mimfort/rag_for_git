from reviewer.graph.summaries import Member, build_clusters, depth_for


def test_depth_for_no_overrides_returns_default():
    assert depth_for("a/b/c.py", 2, {}) == 2


def test_depth_for_longest_prefix_wins():
    ov = {"reviewer": 1, "reviewer/index": 3}
    assert depth_for("reviewer/index/store.py", 2, ov) == 3
    assert depth_for("reviewer/mcp/service.py", 2, ov) == 1


def test_depth_for_sibling_prefix_not_matched():
    assert depth_for("reviewer/indexer/x.py", 2, {"reviewer/index": 3}) == 2


def test_depth_for_root_file_uses_default():
    assert depth_for("setup.py", 2, {"reviewer": 1}) == 2


def test_build_clusters_applies_per_prefix_depth():
    members = [
        Member("reviewer/index/store.py#A", "reviewer/index/store.py", "h1", "sk1", 1),
        Member("reviewer/index/sub/x.py#B", "reviewer/index/sub/x.py", "h2", "sk2", 1),
        Member("reviewer/mcp/service.py#C", "reviewer/mcp/service.py", "h3", "sk3", 1),
    ]
    clusters = build_clusters(members, None, depth=2,
                              depth_overrides={"reviewer/index": 3})
    keys = {c.key for c in clusters}
    assert "reviewer/index" in keys          # store.py: depth 3, директория 2 сегмента
    assert "reviewer/index/sub" in keys      # sub/x.py: depth 3 → 3 сегмента
    assert "reviewer/mcp" in keys            # depth 2 (дефолт)
