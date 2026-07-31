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


def test_delta_sorts_each_classification_by_path():
    current = {"z.py": "new", "a.py": "new"}

    delta = build_fragment_delta("cluster", current, [], bootstrap=False, full_rebuild=False)

    assert [item.path for item in delta.added] == ["a.py", "z.py"]
