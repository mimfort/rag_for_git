import reviewer.services.summary_fragments as summary_fragments
from reviewer.services.summary_fragments import (
    StoredSummaryFragment,
    build_fragment_delta,
)


def test_delta_classifies_added_changed_moved_reused_and_removed():
    current = {
        "same.py": "same",
        "changed.py": "new",
        "added.py": "new",
        "moved.py": "same",
    }
    stored = [
        StoredSummaryFragment("cluster", "same.py", "same", "S", {}),
        StoredSummaryFragment("cluster", "changed.py", "old", "C", {}),
        StoredSummaryFragment("old-cluster", "moved.py", "same", "M", {}),
        StoredSummaryFragment("cluster", "removed.py", "old", "R", {}),
    ]
    delta = build_fragment_delta(
        "cluster", current, stored, bootstrap=False, full_rebuild=False
    )
    assert [item.path for item in delta.added] == ["added.py"]
    assert [item.path for item in delta.changed] == ["changed.py"]
    assert [item.path for item in delta.moved] == ["moved.py"]
    assert [item.path for item in delta.reused] == ["same.py"]
    assert [item.path for item in delta.removed] == ["removed.py"]
    assert delta.pending_paths == ("added.py", "changed.py")


def test_delta_bootstrap_marks_every_current_file_pending():
    current = {"same.py": "same", "changed.py": "new"}
    stored = [
        StoredSummaryFragment("cluster", "same.py", "same", "S", {}),
        StoredSummaryFragment("cluster", "changed.py", "old", "C", {}),
    ]

    delta = build_fragment_delta("cluster", current, stored, bootstrap=True, full_rebuild=False)

    assert [item.path for item in delta.added] == ["changed.py", "same.py"]
    assert not delta.reused
    assert delta.pending_paths == ("changed.py", "same.py")


def test_delta_full_rebuild_marks_every_current_file_pending():
    current = {"same.py": "same", "moved.py": "same"}
    stored = [
        StoredSummaryFragment("cluster", "same.py", "same", "S", {}),
        StoredSummaryFragment("old-cluster", "moved.py", "same", "M", {}),
    ]

    delta = build_fragment_delta("cluster", current, stored, bootstrap=False, full_rebuild=True)

    assert [item.path for item in delta.added] == ["moved.py", "same.py"]
    assert not delta.reused
    assert not delta.moved
    assert delta.pending_paths == ("moved.py", "same.py")


def test_delta_prefers_matching_current_cluster_fragment_among_duplicates():
    current = {"same.py": "same"}
    stored = [
        StoredSummaryFragment("old-cluster", "same.py", "same", "old", {}),
        StoredSummaryFragment("cluster", "same.py", "same", "current", {"source": "db"}),
    ]

    delta = build_fragment_delta("cluster", current, stored, bootstrap=False, full_rebuild=False)

    assert delta.reused[0].summary == "current"
    assert delta.reused[0].provenance == {"source": "db"}


def test_delta_regenerates_ambiguous_exact_cross_cluster_candidates():
    current = {"same.py": "same"}
    stored = [
        StoredSummaryFragment("old-a", "same.py", "same", "A", {}),
        StoredSummaryFragment("old-b", "same.py", "same", "B", {}),
    ]

    delta = build_fragment_delta(
        "target",
        current,
        stored,
        bootstrap=False,
        full_rebuild=False,
    )

    assert [item.path for item in delta.changed] == ["same.py"]
    assert delta.pending_paths == ("same.py",)
    assert not delta.moved


def test_delta_sorts_each_classification_by_path():
    current = {"z.py": "new", "a.py": "new"}

    delta = build_fragment_delta("cluster", current, [], bootstrap=False, full_rebuild=False)

    assert [item.path for item in delta.added] == ["a.py", "z.py"]


def test_server_generation_provenance_overwrites_reserved_client_value():
    assert summary_fragments.with_server_generation_provenance(
        {
            "model": "cheap",
            "_reviewer": {"generation": "forged", "depth": 999},
        },
        2,
        "layout-v2",
    ) == {
        "model": "cheap",
        "_reviewer": {
            "generation": "summary-fragment-v2",
            "layout_token": "layout-v2",
            "depth": 2,
        },
    }


def test_generation_completion_requires_every_exact_same_cluster_fragment_at_depth():
    current = {"a.py": "fp-a", "b.py": "fp-b"}
    stamped = {
        "_reviewer": {
            "generation": "summary-fragment-v2",
            "layout_token": "layout-v2",
            "depth": 2,
        }
    }
    complete = [
        StoredSummaryFragment("cluster", "a.py", "fp-a", "A", stamped),
        StoredSummaryFragment("cluster", "b.py", "fp-b", "B", stamped),
    ]

    assert summary_fragments.has_complete_fragment_generation(
        "cluster", current, complete, 2, "layout-v2"
    )
    assert not summary_fragments.has_complete_fragment_generation(
        "cluster", current, complete[:1], 2, "layout-v2"
    )
    assert not summary_fragments.has_complete_fragment_generation(
        "cluster",
        current,
        [
            complete[0],
            StoredSummaryFragment("other", "b.py", "fp-b", "B", stamped),
        ],
        2,
        "layout-v2",
    )
    assert not summary_fragments.has_complete_fragment_generation(
        "cluster",
        current,
        [
            complete[0],
            StoredSummaryFragment(
                "cluster",
                "b.py",
                "wrong",
                "B",
                stamped,
            ),
        ],
        2,
        "layout-v2",
    )
    assert not summary_fragments.has_complete_fragment_generation(
        "cluster", current, complete, 3, "layout-v2"
    )
    assert not summary_fragments.has_complete_fragment_generation(
        "cluster", current, complete, 2, "other-layout"
    )
    assert not summary_fragments.has_complete_fragment_generation(
        "cluster",
        current,
        [
            *complete,
            StoredSummaryFragment("cluster", "stale.py", "old", "Stale", stamped),
        ],
        2,
        "layout-v2",
    )
