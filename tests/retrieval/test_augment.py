"""Сборка путей-кандидатов из фактических диффов похожих задач (PRI-257).

Co-change (rank_cochanged/commit_file_sets) снят по итогам приёмки — см.
.superpowers/sdd/2026-08-17-pri-257-augmented-candidates/step8-measurement.md,
«Вердикт по критерию приёмки 1».
"""
from reviewer.retrieval.augment import (AugmentResult, collect_similar_task_paths,
                                        collect_subsystem_paths)


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


def test_git_fallback_failure_on_one_key_does_not_skip_the_rest(monkeypatch):
    """Финальное ревью ветки, Minor 1: сбой grep по одному ключу — не break, а continue.

    Раньше исключение на первом ключе/алиасе обрывало весь цикл по lookup:
    отказ, специфичный для одного паттерна, лишал шанса остальные ключи.
    """
    calls: list = []

    def flaky_grep(repo, pattern, *, limit):
        calls.append(pattern)
        if pattern == "PRI-1":
            raise RuntimeError("паттерн не разобрался")
        return ["reviewer/b.py"]

    monkeypatch.setattr("reviewer.retrieval.augment.gitutil.paths_touched_by_grep",
                        flaky_grep)
    result = collect_similar_task_paths(
        keys=["PRI-1"], aliases_by_key={"PRI-1": ["ID-311"]},
        history=None, clone_path="/repo", limit=10)
    assert calls == ["PRI-1", "ID-311"], "второй алиас всё равно опрошен"
    assert result.paths == ["reviewer/b.py"]
    assert any("git-история" in gap for gap in result.gaps)


def test_missing_clone_records_a_gap_instead_of_silent_empty_result():
    """Финальное ревью ветки, Minor 2: пустой clone_path — не тихий пропуск, а gap.

    Без записи в gaps пустая выдача augmented неотличима от «источник
    отработал и ничего не нашёл».
    """
    result = collect_similar_task_paths(
        keys=["PRI-1"], aliases_by_key={}, history=None, clone_path="", limit=10)
    assert result.paths == []
    assert any("клон недоступен" in gap for gap in result.gaps)


class _FakeSubsystemSummaryStore:
    def __init__(self, rows=None, fail=False):
        self._rows = rows or []
        self._fail = fail
        self.calls: list = []

    def search_summaries(self, repo, branch, query_embedding, top_k):
        self.calls.append((repo, branch, top_k))
        if self._fail:
            raise RuntimeError("Postgres недоступен")
        return list(self._rows)


class _FakeSubsystemEmbedder:
    def __init__(self, fail=False):
        self._fail = fail

    def embed_query(self, text):
        if self._fail:
            raise RuntimeError("нет квоты")
        return [0.1] * 8


def test_subsystem_paths_are_cluster_members_without_symbol_part():
    store = _FakeSubsystemSummaryStore([
        {"cluster_key": "reviewer/retrieval",
         "member_node_ids": ["reviewer/retrieval/a.py#A", "reviewer/retrieval/a.py#B",
                             "reviewer/retrieval/b.py#C"]},
    ])
    result = collect_subsystem_paths(
        summary_store=store, embedder=_FakeSubsystemEmbedder(), repo="o/n", branch="dev",
        query="q", limit=10)
    assert result.paths == ["reviewer/retrieval/a.py", "reviewer/retrieval/b.py"], \
        "path#fqn срезан до пути, дубли схлопнуты с сохранением порядка"
    assert result.by_source["subsystems"] == 2
    assert store.calls == [("o/n", "dev", 3)], "свой ANN top-N, а не выдача секции subsystems"


def test_subsystem_paths_are_empty_when_summaries_are_cold():
    result = collect_subsystem_paths(
        summary_store=_FakeSubsystemSummaryStore([]), embedder=_FakeSubsystemEmbedder(),
        repo="o/n", branch="dev", query="q", limit=10)
    assert result.paths == []
    assert any("сводки" in gap for gap in result.gaps)


def test_subsystem_paths_survive_store_failure_with_gap():
    result = collect_subsystem_paths(
        summary_store=_FakeSubsystemSummaryStore(fail=True), embedder=_FakeSubsystemEmbedder(),
        repo="o/n", branch="dev", query="q", limit=10)
    assert result.paths == []
    assert any("сводки подсистем недоступны" in gap for gap in result.gaps)


def test_subsystem_paths_survive_embedder_failure_with_gap():
    result = collect_subsystem_paths(
        summary_store=_FakeSubsystemSummaryStore(
            [{"cluster_key": "x", "member_node_ids": ["x/a.py#A"]}]),
        embedder=_FakeSubsystemEmbedder(fail=True), repo="o/n", branch="dev", query="q",
        limit=10)
    assert result.paths == []
    assert any("эмбеддинг" in gap for gap in result.gaps)


def test_subsystem_paths_respect_limit():
    store = _FakeSubsystemSummaryStore([
        {"cluster_key": "c", "member_node_ids": [f"c/f{i}.py#S" for i in range(30)]},
    ])
    result = collect_subsystem_paths(
        summary_store=store, embedder=_FakeSubsystemEmbedder(), repo="o/n", branch="dev",
        query="q", limit=5)
    assert len(result.paths) == 5
