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
         neighbors=None):
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
