"""Подсчёт co-change и сборка путей-кандидатов (PRI-257)."""
from reviewer.retrieval.augment import AugmentResult, rank_cochanged


def test_cochanged_ranks_by_cooccurrence_count():
    commits = [
        {"reviewer/a.py", "reviewer/b.py"},
        {"reviewer/a.py", "reviewer/b.py"},
        {"reviewer/a.py", "reviewer/c.py"},
    ]
    assert rank_cochanged(commits, {"reviewer/a.py"}, min_count=2, limit=10) == [
        "reviewer/b.py"
    ]


def test_cochanged_excludes_seeds_themselves():
    commits = [{"reviewer/a.py", "reviewer/b.py"}] * 3
    ranked = rank_cochanged(commits, {"reviewer/a.py"}, min_count=2, limit=10)
    assert "reviewer/a.py" not in ranked


def test_cochanged_respects_min_count_and_limit():
    commits = [
        {"reviewer/a.py", "reviewer/b.py"},
        {"reviewer/a.py", "reviewer/c.py"},
        {"reviewer/a.py", "reviewer/c.py"},
        {"reviewer/a.py", "reviewer/d.py"},
        {"reviewer/a.py", "reviewer/d.py"},
    ]
    assert rank_cochanged(commits, {"reviewer/a.py"}, min_count=2, limit=1) == [
        "reviewer/c.py"
    ], "порядок при равном счёте — по пути, лимит режет хвост"


def test_cochanged_without_seeds_or_commits_is_empty():
    assert rank_cochanged([], {"reviewer/a.py"}, min_count=2, limit=5) == []
    assert rank_cochanged([{"reviewer/a.py"}], set(), min_count=2, limit=5) == []


def test_augment_result_is_immutable_value():
    result = AugmentResult(paths=["reviewer/a.py"], by_source={"cochange": 1}, gaps=[])
    assert result.paths == ["reviewer/a.py"]
    assert result.by_source["cochange"] == 1
    assert result.gaps == []
