"""Сборка путей-кандидатов из фактических диффов похожих задач (PRI-257).

Co-change (rank_cochanged/commit_file_sets) снят по итогам приёмки — см.
.superpowers/sdd/2026-08-17-pri-257-augmented-candidates/step8-measurement.md,
«Вердикт по критерию приёмки 1».
"""
from reviewer.retrieval.augment import AugmentResult, collect_similar_task_paths


def test_augment_result_is_immutable_value():
    result = AugmentResult(paths=["reviewer/a.py"], by_source={"similar_diffs": 1}, gaps=[])
    assert result.paths == ["reviewer/a.py"]
    assert result.by_source["similar_diffs"] == 1
    assert result.gaps == []


class _FakeHistory:
    def __init__(self, by_key=None, fail=False):
        self._by_key = by_key or {}
        self._fail = fail

    def diff_paths_for_tasks(self, keys, repo=None):
        if self._fail:
            raise RuntimeError("Postgres недоступен")
        return {k: v for k, v in self._by_key.items() if k in keys}


def test_similar_paths_match_by_key_and_alias():
    history = _FakeHistory({"PRI-257": ["reviewer/a.py"]})
    result = collect_similar_task_paths(
        keys=["ID-311"], aliases_by_key={"ID-311": ["PRI-257"]},
        history=history, clone_path="", limit=10)
    assert result.paths == ["reviewer/a.py"]
    assert result.by_source["similar_diffs"] == 1


def test_similar_paths_survive_history_failure_with_gap():
    result = collect_similar_task_paths(
        keys=["ID-311"], aliases_by_key={}, history=_FakeHistory(fail=True),
        clone_path="", limit=10)
    assert result.paths == []
    assert any("история прогонов" in gap for gap in result.gaps)


def test_similar_paths_without_history_fall_back_to_git(monkeypatch):
    calls: list = []

    def fake_grep(repo, pattern, *, limit):
        calls.append(pattern)
        return ["reviewer/b.py"]

    monkeypatch.setattr("reviewer.retrieval.augment.gitutil.paths_touched_by_grep",
                        fake_grep)
    result = collect_similar_task_paths(
        keys=["ID-311"], aliases_by_key={"ID-311": ["PRI-257"]},
        history=None, clone_path="/repo", limit=10)
    assert result.paths == ["reviewer/b.py"]
    assert "PRI-257" in calls, "grep идёт по человеческому ключу, не по ID-N"


def test_similar_paths_respect_limit():
    history = _FakeHistory({"PRI-1": [f"reviewer/f{i}.py" for i in range(10)]})
    result = collect_similar_task_paths(
        keys=["PRI-1"], aliases_by_key={}, history=history, clone_path="", limit=3)
    assert len(result.paths) == 3
