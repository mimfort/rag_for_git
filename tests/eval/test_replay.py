"""Оркестрация replay-прогона корпуса (PRI-254)."""
from __future__ import annotations

import pathlib

from eval.solve_task_metrics import replay

BRIEF = """# Brief — {key}

## Relevant code
- `reviewer/whatever.py:1` — неважно: replay не читает эту секцию.
"""


def _corpus(tmp_path: pathlib.Path, keys) -> pathlib.Path:
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    for index, key in enumerate(keys):
        (briefs / f"2026-01-{index + 1:02d}-{key}-x.md").write_text(
            BRIEF.format(key=key), encoding="utf-8"
        )
    return briefs


class FakeGit:
    """git-раннер с заранее заданными мержами и составом diff'а.

    Для сидов контекстного ядра (PRI-261/262) отдаёт настоящий unified diff с
    заголовком `@@` и парсибельный Python-источник — иначе `collect_seeds`
    (через `parse_hunks`/`chunk_python`) находил бы ноль сидов и тесты
    контекстного ядра проходили бы вхолостую, ничего не проверяя.
    """

    # Один и тот же символ на любой core-путь. Изменённые строки обязаны и
    # задавать символ-сид ("caller"), и НАЗЫВАТЬ вызов ("g"): с PRI-262 сосед
    # проходит фильтр, только если его имя прозвучало на изменённой строке,
    # и источник без единого вызова оставил бы ядро пустым при живых сидах.
    SOURCE = "def caller():\n    return g()\n"
    HUNK = "@@ -0,0 +1,2 @@\n+def caller():\n+    return g()\n"

    def __init__(self, changed_by_key, missing=()):
        self.changed_by_key = changed_by_key
        self.missing = set(missing)

    def __call__(self, args):
        if args[0] == "log":
            key = args[-1].removeprefix("--grep=")
            if key in self.missing or key not in self.changed_by_key:
                return ""
            return f"sha{key} Merge pull request #1 from owner/branch\n"
        if args[0] == "diff" and "--name-only" in args:
            key = args[-1].removeprefix("sha")
            return "\n".join(self.changed_by_key.get(key, []))
        if args[0] == "diff" and "--unified=0" in args:
            # ["diff", "--unified=0", "sha<key>^1", "sha<key>", "--", path]
            return self.HUNK
        if args[0] == "show":
            # ["show", "sha<key>:<path>"]
            return self.SOURCE
        if args[0] == "cat-file":
            return ""
        raise AssertionError(f"неожиданный git-вызов: {args}")


class FakeProvider:
    def __init__(self, tasks, paths_by_key, fail=(), neighbors=None):
        self.tasks = tasks
        self.paths_by_key = paths_by_key
        self.fail = set(fail)
        self.queries: list = []
        self._neighbors = neighbors if neighbors is not None else set()

    def neighbors(self, repo, branch, node_ids):
        return self._neighbors

    def preflight(self, repo, branch):
        return {
            "branch": branch,
            "indexed_sha": "abc123",
            "drift": 0,
            "summaries": 1,
            "chunks": 2,
            "graph_nodes": 3,
        }

    def task(self, key):
        return self.tasks.get(key)

    def query(self, task, key):
        return f"{key}|{(task or {}).get('title', '')}"

    def code(self, repo, branch, query, limits):
        self.queries.append(query)
        key = query.split("|")[0]
        if key in self.fail:
            raise RuntimeError("ретрив недоступен")
        return "\n".join(
            f"// {path}#f ({path}:1-2)\n    1 | x = 1"
            for path in self.paths_by_key.get(key, [])
        )


def _target():
    return replay.variants.ReplayTarget(repo="o/n", branch="dev", limits=None)


def _run(tmp_path, keys, *, tasks, changed, predicted, fail=(), missing=(), limit=None,
         neighbors=None, seed_mode=replay.SEED_MODE_LINES):
    provider = FakeProvider(tasks, predicted, fail=fail, neighbors=neighbors)
    return replay.run_replay(
        provider=provider,
        run_git=FakeGit(changed, missing=missing),
        briefs_dir=_corpus(tmp_path, keys),
        target=_target(),
        variant_name="baseline",
        commit="deadbee",
        taken_at="2026-08-17T00:00:00+00:00",
        limit=limit,
        seed_mode=seed_mode,
    )


def test_measured_task_scores_core_recall(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-1"],
        tasks={"PRI-1": {"title": "PRI-1", "description": ""}},
        changed={"PRI-1": ["reviewer/a.py", "reviewer/b.py", "docs/x.md"]},
        predicted={"PRI-1": ["reviewer/a.py"]},
    )
    row = snap["tasks"][0]
    assert row["status"] == replay.STATUS_MEASURED
    assert row["expected_core"] == 2 and row["hit_core"] == 1
    assert row["core_recall"] == 0.5
    assert row["predicted_paths"] == ["reviewer/a.py"]
    assert snap["aggregate"]["core_recall_median"] == 0.5


def test_empty_core_denominator_is_not_zero_recall(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-2"],
        tasks={"PRI-2": {"title": "t", "description": ""}},
        changed={"PRI-2": ["docs/x.md", "tests/test_y.py"]},
        predicted={"PRI-2": []},
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_EMPTY_CORE
    assert snap["tasks"][0]["core_recall"] is None
    assert snap["aggregate"]["n_measured"] == 0


def test_missing_ground_truth_is_named_not_dropped(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-3"],
        tasks={"PRI-3": {"title": "t", "description": ""}},
        changed={},
        predicted={},
        missing=["PRI-3"],
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_NO_GROUND_TRUTH
    assert snap["statuses"][replay.STATUS_NO_GROUND_TRUTH] == 1


def test_task_absent_from_store_is_named(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-4"],
        tasks={},
        changed={"PRI-4": ["reviewer/a.py"]},
        predicted={"PRI-4": ["reviewer/a.py"]},
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_NO_TASK


def test_retrieval_failure_does_not_abort_the_run(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-5", "PRI-6"],
        tasks={
            "PRI-5": {"title": "PRI-5", "description": ""},
            "PRI-6": {"title": "PRI-6", "description": ""},
        },
        changed={"PRI-5": ["reviewer/a.py"], "PRI-6": ["reviewer/b.py"]},
        predicted={"PRI-6": ["reviewer/b.py"]},
        fail=["PRI-5"],
    )
    by_key = {row["key"]: row["status"] for row in snap["tasks"]}
    assert by_key["PRI-5"] == replay.STATUS_RETRIEVAL_FAILED
    assert by_key["PRI-6"] == replay.STATUS_MEASURED


def test_duplicate_keys_counted_once(tmp_path):
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    for name in ("2026-01-01-PRI-7-a.md", "2026-01-02-PRI-7-b.md"):
        (briefs / name).write_text(BRIEF.format(key="PRI-7"), encoding="utf-8")
    assert replay.corpus_keys(briefs) == ["PRI-7"]


def test_limit_truncates_corpus_and_marks_snapshot_partial(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-8", "PRI-9"],
        tasks={"PRI-8": {"title": "PRI-8", "description": ""}},
        changed={"PRI-8": ["reviewer/a.py"]},
        predicted={"PRI-8": ["reviewer/a.py"]},
        limit=1,
    )
    assert snap["partial"] is True
    assert len(snap["tasks"]) == 1


def test_full_run_is_not_partial_and_records_index_identity(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-10"],
        tasks={"PRI-10": {"title": "PRI-10", "description": ""}},
        changed={"PRI-10": ["reviewer/a.py"]},
        predicted={"PRI-10": ["reviewer/a.py"]},
    )
    assert snap["partial"] is False
    assert snap["indexed_sha"] == "abc123"
    assert snap["commit"] == "deadbee"
    assert snap["variant"] == "baseline"


def test_context_core_fields_present_in_row(tmp_path):
    """Контекстное ядро считается рядом с core, своим статусом и своими путями.

    changed={"reviewer/a.py"} даёт через FakeGit реальный сид
    "reviewer/a.py#g" (настоящий hunk + парсибельный источник, см. FakeGit);
    провайдер-заглушка отдаёт соседа "reviewer/b.py#g" независимо от того,
    какие сиды ему передали — это и есть подставной обход графа. derive_context_core
    вычитает изменённое ядро ("reviewer/a.py") из путей соседей, поэтому
    непустой результат доказывает, что сиды реально нашлись и дошли до обхода,
    а не что тест проходит вхолостую.
    """
    snap = _run(
        tmp_path,
        ["PRI-11"],
        tasks={"PRI-11": {"title": "PRI-11", "description": ""}},
        changed={"PRI-11": ["reviewer/a.py"]},
        predicted={"PRI-11": ["reviewer/a.py"]},
        neighbors={"reviewer/b.py#g"},
    )
    row = snap["tasks"][0]
    assert row["context_status"] == replay.STATUS_MEASURED
    assert row["context_core_paths"] == ["reviewer/b.py"]
    assert row["context_recall"] is not None


def test_empty_context_core_is_not_zero_recall(tmp_path):
    """Пустое контекстное ядро — отдельный статус и None, по образцу
    empty_core_denominator."""
    snap = _run(
        tmp_path,
        ["PRI-12"],
        tasks={"PRI-12": {"title": "PRI-12", "description": ""}},
        changed={"PRI-12": ["reviewer/a.py"]},
        predicted={"PRI-12": ["reviewer/a.py"]},
        neighbors=set(),
    )
    row = snap["tasks"][0]
    assert row["context_status"] == replay.STATUS_EMPTY_CONTEXT
    assert row["context_recall"] is None


class FailingNeighbours:
    """Провайдер, у которого падает ровно обход графа (Neo4j недоступен)."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def neighbors(self, repo, branch, node_ids):
        raise RuntimeError("граф недоступен")


def _run_with_failing_graph(tmp_path, keys, *, tasks, changed, predicted):
    provider = FailingNeighbours(FakeProvider(tasks, predicted))
    return replay.run_replay(
        provider=provider,
        run_git=FakeGit(changed),
        briefs_dir=_corpus(tmp_path, keys),
        target=_target(),
        variant_name="baseline",
        commit="deadbee",
        taken_at="2026-08-17T00:00:00+00:00",
    )


def test_graph_failure_is_named_not_confused_with_empty_core(tmp_path):
    """Сбой обхода отличим от честно пустого ядра — иначе тонет в штатном шуме."""
    snap = _run_with_failing_graph(
        tmp_path,
        ["PRI-21"],
        tasks={"PRI-21": {"title": "PRI-21", "description": ""}},
        changed={"PRI-21": ["reviewer/a.py"]},
        predicted={"PRI-21": ["reviewer/a.py"]},
    )
    row = snap["tasks"][0]
    assert row["context_status"] == replay.STATUS_CONTEXT_FAILED
    assert row["context_status"] != replay.STATUS_EMPTY_CONTEXT
    assert row["context_recall"] is None
    # прогон корпуса не прерван: задача измерена по core как обычно
    assert row["status"] == replay.STATUS_MEASURED


def test_graph_failure_is_counted_in_context_statuses(tmp_path):
    snap = _run_with_failing_graph(
        tmp_path,
        ["PRI-22"],
        tasks={"PRI-22": {"title": "PRI-22", "description": ""}},
        changed={"PRI-22": ["reviewer/a.py"]},
        predicted={"PRI-22": ["reviewer/a.py"]},
    )
    assert snap["context_statuses"][replay.STATUS_CONTEXT_FAILED] == 1
    assert snap["context_statuses"][replay.STATUS_EMPTY_CONTEXT] == 0


def test_context_statuses_count_only_tasks_that_reached_the_traversal(tmp_path):
    """Задача без ground truth до обхода не доходит и счётчик не подкрашивает."""
    snap = _run(
        tmp_path,
        ["PRI-23"],
        tasks={"PRI-23": {"title": "PRI-23", "description": ""}},
        changed={},
        predicted={},
        missing=["PRI-23"],
    )
    assert snap["tasks"][0]["status"] == replay.STATUS_NO_GROUND_TRUTH
    assert sum(snap["context_statuses"].values()) == 0


def test_context_failure_does_not_move_any_existing_number(tmp_path):
    """Свойство аддитивности на случайных входах (критерий 3 PRI-265).

    Сторона «до» — обход, честно вернувший пусто; сторона «после» — упавший
    обход. Ни одно существующее число агрегата и ни один счётчик STATUSES не
    имеет права разойтись: новый статус добавляет знание, а не меняет метрику.

    Первый ключ набора всегда получает и changed, и predicted безусловно:
    иначе на наборе, где ни у одного ключа случайно не оказалось ground
    truth, обе стороны дают нулевой context_statuses и последний ассерт
    флейкует на равенстве пустых словарей.
    """
    import random

    rnd = random.Random(20265)
    for index in range(25):
        keys = [f"PRI-{i}" for i in range(1, rnd.randrange(2, 6))]
        first, rest = keys[0], keys[1:]
        tasks = {k: {"title": k, "description": ""} for k in keys}
        changed = {first: ["reviewer/a.py"]}
        changed.update({k: ["reviewer/a.py"] for k in rest if rnd.random() < 0.8})
        predicted = {first: ["reviewer/a.py"]}
        predicted.update({k: ["reviewer/a.py"] for k in rest if rnd.random() < 0.7})
        empty_dir = tmp_path / f"e{index}"
        failed_dir = tmp_path / f"f{index}"
        empty_dir.mkdir()
        failed_dir.mkdir()
        empty = _run(empty_dir, keys, tasks=tasks, changed=changed,
                     predicted=predicted, neighbors=set())
        failed = _run_with_failing_graph(failed_dir, keys, tasks=tasks,
                                         changed=changed, predicted=predicted)
        assert failed["aggregate"] == empty["aggregate"]
        assert failed["statuses"] == empty["statuses"]
        assert failed["context_statuses"] != empty["context_statuses"]


def test_aggregate_carries_context_medians(tmp_path):
    snap = _run(
        tmp_path,
        ["PRI-13"],
        tasks={"PRI-13": {"title": "PRI-13", "description": ""}},
        changed={"PRI-13": ["reviewer/a.py"]},
        predicted={"PRI-13": ["reviewer/a.py"]},
        neighbors={"reviewer/b.py#g"},
    )
    agg = snap["aggregate"]
    assert "context_recall_median" in agg
    assert "union_precision_median" in agg
    assert agg["context_n_measured"] >= 0


def test_seed_mode_lines_is_the_default_and_ignores_signature_names(monkeypatch, tmp_path):
    """Дефолт тождественен поведению до PRI-266: шапка не участвует."""
    seen = {}

    def fake_collect(truth, run_git):
        return replay.context_seeds.SeedSet(
            symbols={"reviewer/a.py#g"},
            called_names={"g"},
            signature_names={"Sig"},
        )

    def fake_derive(seed_ids, changed_core, traverse, allowed_names=None):
        seen["allowed"] = allowed_names
        return set()

    monkeypatch.setattr(replay.context_seeds, "collect_seeds", fake_collect)
    monkeypatch.setattr(replay.context_core, "derive_context_core", fake_derive)
    _run(
        tmp_path,
        ["PRI-31"],
        tasks={"PRI-31": {"title": "PRI-31", "description": ""}},
        changed={"PRI-31": ["reviewer/a.py"]},
        predicted={"PRI-31": ["reviewer/a.py"]},
        neighbors=set(),
    )
    assert seen["allowed"] == {"g"}


def test_seed_mode_signature_unions_both_name_sources(monkeypatch, tmp_path):
    seen = {}

    def fake_collect(truth, run_git):
        return replay.context_seeds.SeedSet(
            symbols={"reviewer/a.py#g"},
            called_names={"g"},
            signature_names={"Sig"},
        )

    def fake_derive(seed_ids, changed_core, traverse, allowed_names=None):
        seen["allowed"] = allowed_names
        return set()

    monkeypatch.setattr(replay.context_seeds, "collect_seeds", fake_collect)
    monkeypatch.setattr(replay.context_core, "derive_context_core", fake_derive)
    _run(
        tmp_path,
        ["PRI-32"],
        tasks={"PRI-32": {"title": "PRI-32", "description": ""}},
        changed={"PRI-32": ["reviewer/a.py"]},
        predicted={"PRI-32": ["reviewer/a.py"]},
        neighbors=set(),
        seed_mode=replay.SEED_MODE_LINES_SIGNATURE,
    )
    assert seen["allowed"] == {"g", "Sig"}


def test_lines_mode_moves_no_existing_number(tmp_path):
    """Аддитивность: режим по умолчанию оставляет строку прогона побайтово
    той же, какой она была до PRI-266. Иначе числа приёмок PRI-255…262
    перестают быть сравнимыми без пересчёта."""
    kwargs = dict(
        tasks={"PRI-34": {"title": "PRI-34", "description": ""}},
        changed={"PRI-34": ["reviewer/a.py"]},
        predicted={"PRI-34": ["reviewer/a.py"]},
        neighbors={"reviewer/b.py#g"},
    )
    left, right = tmp_path / "a", tmp_path / "b"
    left.mkdir()
    right.mkdir()
    default_row = _run(left, ["PRI-34"], **kwargs)["tasks"][0]
    explicit_row = _run(
        right, ["PRI-34"], seed_mode=replay.SEED_MODE_LINES, **kwargs
    )["tasks"][0]
    assert default_row == explicit_row


def test_snapshot_records_the_seed_mode(tmp_path):
    """Режим сидов виден в снимке: без него две стороны A/B неразличимы."""
    snap = _run(
        tmp_path,
        ["PRI-33"],
        tasks={"PRI-33": {"title": "PRI-33", "description": ""}},
        changed={"PRI-33": ["reviewer/a.py"]},
        predicted={"PRI-33": ["reviewer/a.py"]},
        neighbors=set(),
        seed_mode=replay.SEED_MODE_LINES_SIGNATURE,
    )
    assert snap["seed_mode"] == replay.SEED_MODE_LINES_SIGNATURE
