from reviewer.graph.summaries import (
    Member,
    build_clusters,
    cluster_key,
    compute_file_fingerprints,
    compute_layout_token,
    compute_source_hash,
)


def _m(node_id, path, h="h", line=1, sk=None):
    return Member(node_id=node_id, path=path, content_hash=h, start_line=line,
                  skeleton_hash=sk if sk is not None else h)


def test_cluster_key_takes_first_depth_dir_segments():
    assert cluster_key("reviewer/index/store.py", 2) == "reviewer/index"
    assert cluster_key("reviewer/graph/store.py", 2) == "reviewer/graph"
    assert cluster_key("reviewer/index/sub/x.py", 2) == "reviewer/index"


def test_cluster_key_root_file_and_short_dir():
    assert cluster_key("setup.py", 2) == "<root>"
    assert cluster_key("reviewer/app.py", 2) == "reviewer"


def test_compute_source_hash_is_order_independent_and_changes_with_content():
    a = compute_source_hash([("x#f", "h1"), ("y#g", "h2")])
    b = compute_source_hash([("y#g", "h2"), ("x#f", "h1")])
    assert a == b                       # детерминирован, не зависит от порядка
    assert a != compute_source_hash([("x#f", "h1"), ("y#g", "CHANGED")])


def test_layout_token_is_canonical_and_covers_default_and_sorted_overrides():
    token = compute_layout_token(
        2,
        {"reviewer/mcp": 1, "reviewer/index": 3},
    )

    assert token == compute_layout_token(
        2,
        {"reviewer/index": 3, "reviewer/mcp": 1},
    )
    assert token != compute_layout_token(
        3,
        {"reviewer/index": 3, "reviewer/mcp": 1},
    )
    assert token != compute_layout_token(
        2,
        {"reviewer/index": 3},
    )


def test_compute_file_fingerprints_uses_skeleton_per_path():
    members = [
        Member("pkg/a.py#B", "pkg/a.py", "body-1", "sk-b", 8),
        Member("pkg/a.py#A", "pkg/a.py", "body-2", "sk-a", 1),
        Member("pkg/b.py#C", "pkg/b.py", "body-3", "sk-c", 1),
    ]
    got = compute_file_fingerprints(members)
    assert got["pkg/a.py"] == compute_source_hash([
        ("pkg/a.py#A", "sk-a"), ("pkg/a.py#B", "sk-b")
    ])
    assert set(got) == {"pkg/a.py", "pkg/b.py"}


def test_compute_file_fingerprints_ignores_body_changes_but_not_skeleton_changes():
    base = [Member("pkg/a.py#A", "pkg/a.py", "body-1", "sk-a", 1)]
    body_changed = [Member("pkg/a.py#A", "pkg/a.py", "body-2", "sk-a", 1)]
    skeleton_changed = [Member("pkg/a.py#A", "pkg/a.py", "body-1", "sk-b", 1)]

    assert compute_file_fingerprints(body_changed) == compute_file_fingerprints(base)
    assert compute_file_fingerprints(skeleton_changed) != compute_file_fingerprints(base)


def test_build_clusters_groups_by_module_and_filters_min_size():
    members = [
        _m("reviewer/index/a.py#A", "reviewer/index/a.py"),
        _m("reviewer/index/b.py#B", "reviewer/index/b.py"),
        _m("reviewer/graph/c.py#C", "reviewer/graph/c.py"),
    ]
    clusters = build_clusters(members, None, depth=2, min_size=2)
    keys = {c.key for c in clusters}
    assert keys == {"reviewer/index"}     # graph отброшен (1 < min_size=2)
    idx = next(c for c in clusters if c.key == "reviewer/index")
    assert idx.num_members == 2
    assert idx.files == ["reviewer/index/a.py", "reviewer/index/b.py"]


def test_build_clusters_ranks_top_symbols_by_in_degree():
    members = [
        _m("reviewer/x/a.py#A", "reviewer/x/a.py", line=10),
        _m("reviewer/x/b.py#B", "reviewer/x/b.py", line=20),
    ]
    deg = {"reviewer/x/b.py#B": 5, "reviewer/x/a.py#A": 1}
    [c] = build_clusters(members, lambda ids: deg, depth=2, top_n=10)
    assert c.top_symbols[0]["node_id"] == "reviewer/x/b.py#B"   # выше in_degree → первый


def test_build_clusters_fail_soft_when_in_degree_raises():
    members = [_m("reviewer/x/a.py#A", "reviewer/x/a.py")]
    def boom(ids):
        raise RuntimeError("neo4j down")
    [c] = build_clusters(members, boom, depth=2)   # не падает
    assert c.top_symbols[0]["node_id"] == "reviewer/x/a.py#A"


def test_build_clusters_source_hash_uses_skeleton_not_body():
    base = [_m("p/a.py#A", "p/a.py", h="body1", sk="sig1"),
            _m("p/b.py#B", "p/b.py", h="body2", sk="sig2")]
    [c0] = build_clusters(base, None, depth=1)
    # правка только тела (skeleton тот же) → тот же source_hash
    body = [_m("p/a.py#A", "p/a.py", h="BODYX", sk="sig1"),
            _m("p/b.py#B", "p/b.py", h="body2", sk="sig2")]
    [c1] = build_clusters(body, None, depth=1)
    assert c1.source_hash == c0.source_hash
    # смена сигнатуры (skeleton изменился) → другой source_hash
    sig = [_m("p/a.py#A", "p/a.py", h="body1", sk="SIGX"),
           _m("p/b.py#B", "p/b.py", h="body2", sk="sig2")]
    [c2] = build_clusters(sig, None, depth=1)
    assert c2.source_hash != c0.source_hash
