"""Unit-тесты сборки среза по корпусу брифов (git инъектирован, сети нет)."""
from eval.solve_task_metrics import history, snapshot
from eval.solve_task_metrics.__main__ import resolve_config, resolve_paths
from eval.solve_task_metrics.config import DEFAULT

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
        config=DEFAULT,
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
        taken_at="2026-08-14T00:00:00+00:00", config=DEFAULT,
    )

    assert snap["cost"]["weighted_median"] < snap["cost"]["raw_median"]
    assert snap["cost"]["inflation"] > 1.0


def test_build_snapshot_counts_new_file_miss(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-x.md").write_text(BRIEF, encoding="utf-8")

    snap, _ = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00", config=DEFAULT,
    )

    assert snap["misses"]["новый файл (не существовал до PR)"] == 1
    assert snap["misses"]["tests/"] == 1


def test_build_snapshot_brief_without_key_not_in_quality(tmp_path):
    (tmp_path / "2026-01-01-noключа.md").write_text(BRIEF, encoding="utf-8")

    snap, rows = snapshot.build_snapshot(
        briefs_dir=tmp_path, run_git=_fake_git, commit="c",
        taken_at="2026-08-14T00:00:00+00:00", config=DEFAULT,
    )

    assert rows == []
    assert snap["corpus"]["with_ground_truth"] == 0


def test_build_snapshot_counts_one_key_once(tmp_path):
    """Два брифа с одним ключом — одна задача, а не двойной вес в агрегате."""
    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()
    body = (
        "# Бриф — PRI-1\n\n## Relevant code\n- `reviewer/a.py:1` — деталь\n"
    )
    (briefs_dir / "2026-01-01-PRI-1-first.md").write_text(body, encoding="utf-8")
    (briefs_dir / "2026-01-02-PRI-1-second.md").write_text(body, encoding="utf-8")

    def fake_git(args):
        if args[0] == "log":
            return "aaa111 Merge pull request #1 from o/b\n"
        if args[0] == "diff":
            return "reviewer/a.py\n"
        return ""

    snap, rows = snapshot.build_snapshot(
        briefs_dir=briefs_dir,
        run_git=fake_git,
        commit="deadbee",
        taken_at="2026-01-03T00:00:00+00:00",
        config=DEFAULT,
    )

    assert snap["corpus"]["briefs"] == 2
    assert snap["corpus"]["with_key"] == 1
    assert len(rows) == 1


def test_snapshot_uses_foreign_repo_config(tmp_path):
    """--repo-path чужого клона: ядро и ключ берутся из ЕГО .review.yml."""
    (tmp_path / ".review.yml").write_text(
        "task_board:\n  key_pattern: 'RON-\\d+'\n"
        "metrics:\n  brief_quality:\n    core_paths: ['app/**/*.py']\n",
        encoding="utf-8")
    config = resolve_config(tmp_path, briefs_dir=None)      # хелпер __main__
    assert config.key_pattern == r"RON-\d+"
    assert config.matches_core("app/api/routes.py") is True


def test_history_path_follows_repo_path(tmp_path):
    """Ряды чужого репозитория не смешиваются с нашими замерами приёмок."""
    paths = resolve_paths(tmp_path, briefs_dir=None)
    assert paths.history == tmp_path / "eval" / history.HISTORY_PATH_NAME
    assert paths.briefs == tmp_path / "docs" / "superpowers" / "briefs"


def test_relative_briefs_dir_resolves_inside_target_repo(tmp_path):
    """Относительный --briefs-dir — путь ВНУТРИ целевого клона, не текущего каталога.

    Фикс-раунд 1: `pathlib.Path(briefs_dir)` без префикса `repo` резолвился от CWD
    процесса и молча давал пустой корпус вместо ошибки.
    """
    paths = resolve_paths(tmp_path, briefs_dir="docs/briefs")
    assert paths.briefs == tmp_path / "docs" / "briefs"


def test_absolute_briefs_dir_used_as_is(tmp_path):
    """Абсолютный --briefs-dir не должен ловить второй префикс от repo."""
    absolute = tmp_path / "elsewhere" / "briefs"
    paths = resolve_paths(tmp_path / "repo", briefs_dir=str(absolute))
    assert paths.briefs == absolute
