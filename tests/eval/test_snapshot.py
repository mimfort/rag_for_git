"""Unit-тесты сборки среза по корпусу брифов (git инъектирован, сети нет)."""
from eval.solve_task_metrics import history, snapshot

BRIEF = """# Brief — PRI-7 пример

## Relevant code
- `reviewer/a.py:1` — трогаем
- `reviewer/z.py:2` — не трогаем

## Токены (этап solve-task)
Модель: m
fresh-in 10K · out 100K · cache-write 200K · cache-read 2M
Всего: 2.3M токенов
"""


def _fake_git(args):
    if args[0] == "log":
        return "aaa Merge pull request #1 from mimfort/feature/pri-7\n"
    if args[0] == "diff":
        return "reviewer/a.py\nreviewer/b.py\ntests/test_a.py\ndocs/x.md\n"
    if args[0] == "cat-file":
        # reviewer/b.py — новый файл, остальные существовали
        if args[2].endswith(":reviewer/b.py"):
            raise snapshot.ground_truth.GitError("нет объекта")
        return ""
    raise AssertionError(f"неожиданный git-вызов: {args}")


def test_build_snapshot_counts_corpus_and_metrics(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-x.md").write_text(BRIEF, encoding="utf-8")

    snap, rows = snapshot.build_snapshot(
        briefs_dir=tmp_path,
        run_git=_fake_git,
        commit="deadbee",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert snap["schema"] == history.SCHEMA
    assert snap["commit"] == "deadbee"
    assert snap["corpus"]["briefs"] == 1
    assert snap["corpus"]["with_tokens"] == 1
    # знаменатель ядра — только reviewer/a.py (b.py новый, tests/docs вне ядра)
    assert rows[0]["expected_core"] == 1
    assert rows[0]["core_recall"] == 1.0
    assert snap["quality"]["core_recall_median"] == 1.0


def test_build_snapshot_weighted_cost_is_below_raw(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-x.md").write_text(BRIEF, encoding="utf-8")

    snap, _ = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert snap["cost"]["weighted_median"] < snap["cost"]["raw_median"]
    assert snap["cost"]["inflation"] > 1.0


def test_build_snapshot_counts_new_file_miss(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-x.md").write_text(BRIEF, encoding="utf-8")

    snap, _ = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert snap["misses"]["новый файл (не существовал до PR)"] == 1
    assert snap["misses"]["tests/"] == 1


def test_build_snapshot_brief_without_key_not_in_quality(tmp_path):
    (tmp_path / "2026-01-01-noключа.md").write_text(BRIEF, encoding="utf-8")

    snap, rows = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00",
    )

    assert rows == []
    assert snap["corpus"]["with_ground_truth"] == 0
