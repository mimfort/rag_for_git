"""Тесты адаптера онлайн-метрики качества брифа (PRI-249).

Файловая система — только tmp_path; ни БД, ни git, ни сети.
"""
from __future__ import annotations

import pytest

from reviewer.metrics.brief_quality import classify, recall
from reviewer.metrics.brief_quality.config import DEFAULT
from reviewer.services.brief_quality import BRIEFS_DIR, find_brief, measure

_BRIEF = """# Brief — PRI-999 Тестовая задача

## Relevant code
- `reviewer/mcp/service.py:100` — точка съёма
- `reviewer/web/history.py:94` — запись истории
- `docs/superpowers/specs/old.md` — не ядро
(dropped 3: не информируют реализацию)

## Test exemplars
- `tests/web/test_history.py:84` — образец
"""


def _clone(tmp_path, name="2026-08-14-PRI-999-test.md", text=_BRIEF):
    briefs = tmp_path / BRIEFS_DIR
    briefs.mkdir(parents=True, exist_ok=True)
    (briefs / name).write_text(text, encoding="utf-8")
    return str(tmp_path)


def test_measured_matches_offline_formula(tmp_path):
    """core-recall считается по знаменателю «ядро И существовал до PR»."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=[
            "reviewer/mcp/service.py",      # ядро, существовал, предсказан
            "reviewer/web/history.py",      # ядро, существовал, предсказан
            "reviewer/web/api.py",          # ядро, существовал, НЕ предсказан
            "reviewer/metrics/new.py",      # ядро, но новый файл → вне знаменателя
            "tests/web/test_api.py",        # не ядро
            "README.md",                    # не ядро
        ],
        changed_status={
            "reviewer/mcp/service.py": "modified",
            "reviewer/web/history.py": "modified",
            "reviewer/web/api.py": "modified",
            "reviewer/metrics/new.py": "added",
            "tests/web/test_api.py": "added",
            "README.md": "modified",
        },
    )
    assert result.status == "measured"
    assert result.task_key == "PRI-999"
    assert result.brief_path == f"{BRIEFS_DIR}/2026-08-14-PRI-999-test.md"
    assert result.expected == 6
    assert result.expected_core == 3
    assert result.hit_core == 2
    assert result.core_recall == pytest.approx(2 / 3)
    assert set(result.expected_core_paths) == {
        "reviewer/mcp/service.py",
        "reviewer/web/history.py",
        "reviewer/web/api.py",
    }
    assert set(result.hit_core_paths) == {
        "reviewer/mcp/service.py",
        "reviewer/web/history.py",
    }


def test_misses_are_categorized(tmp_path):
    """Каждый непредсказанный файл попадает в именованную категорию."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=["reviewer/web/api.py", "tests/web/test_api.py", "reviewer/metrics/new.py"],
        changed_status={
            "reviewer/web/api.py": "modified",
            "tests/web/test_api.py": "modified",
            "reviewer/metrics/new.py": "added",
        },
    )
    assert result.misses["reviewer/web"] == 1
    assert result.misses["tests/"] == 1
    assert result.misses["новый файл (не существовал до PR)"] == 1


def test_dropped_line_is_not_a_path(tmp_path):
    """Служебная строка '(dropped N: …)' не попадает в predicted."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=["reviewer/web/api.py"],
        changed_status={"reviewer/web/api.py": "modified"},
    )
    assert all("dropped" not in path for path in result.predicted_paths)


def test_empty_core_denominator_is_not_zero_recall(tmp_path):
    """Diff только из тестов и доков — «нет точки измерения», а не ноль."""
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=["tests/web/test_api.py", "README.md"],
        changed_status={"tests/web/test_api.py": "modified", "README.md": "modified"},
    )
    assert result.status == "empty_core_denominator"
    assert result.core_recall is None


def test_no_task_key(tmp_path):
    result = measure(task_key=None, clone_path=_clone(tmp_path), changed_paths=[], changed_status={})
    assert result.status == "no_task_key"


def test_no_brief_without_clone():
    result = measure(
        task_key="PRI-999", clone_path=None,
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "no_brief"


def test_no_brief_when_key_not_found(tmp_path):
    result = measure(
        task_key="PRI-111", clone_path=_clone(tmp_path),
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "no_brief"


def test_brief_without_relevant_section_is_unreadable(tmp_path):
    clone = _clone(tmp_path, text="# Brief — PRI-999\n\n## Task\nбез секции кода\n")
    result = measure(
        task_key="PRI-999", clone_path=clone,
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "brief_unreadable"


def test_latest_brief_wins_on_duplicate_keys(tmp_path):
    """Несколько брифов на один ключ → берётся лексикографически последний."""
    clone = _clone(tmp_path, name="2026-08-10-PRI-999-old.md")
    _clone(tmp_path, name="2026-08-14-PRI-999-new.md")
    found = find_brief(clone, "PRI-999")
    assert found is not None and found.name == "2026-08-14-PRI-999-new.md"


def test_key_match_is_case_insensitive(tmp_path):
    clone = _clone(tmp_path, name="2026-08-14-pri-999-lower.md")
    assert find_brief(clone, "PRI-999") is not None


def test_key_match_respects_token_boundary(tmp_path):
    """PRI-99 не должен совпасть с брифом PRI-999: ошибка была бы тихой."""
    clone = _clone(tmp_path, name="2026-08-14-PRI-999-test.md")
    assert find_brief(clone, "PRI-99") is None
    assert find_brief(clone, "PRI-999") is not None


def test_brief_in_broken_encoding_is_unreadable(tmp_path):
    """Бриф не в UTF-8 → status, а не исключение наружу."""
    briefs = tmp_path / BRIEFS_DIR
    briefs.mkdir(parents=True, exist_ok=True)
    (briefs / "2026-08-14-PRI-998-cp1251.md").write_bytes(
        "# Brief — PRI-998\n\n## Relevant code\n- `reviewer/web/api.py:1` — путь\n".encode(
            "cp1251"
        )
        + b"\xff\xfe\xfa"
    )
    result = measure(
        task_key="PRI-998", clone_path=str(tmp_path),
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "brief_unreadable"
    assert result.brief_path == f"{BRIEFS_DIR}/2026-08-14-PRI-998-cp1251.md"


def test_no_brief_on_broken_path_not_raising(tmp_path, monkeypatch):
    """`find_brief` не бросает даже на битом/недоступном пути клона (finding 7):
    `is_dir()`/`glob()` могут кинуть OSError/ValueError (null-байт в пути,
    снятые права на каталог) до начала защищённого чтения брифа."""
    import pathlib

    def _broken_is_dir(self):
        raise OSError("permission denied")

    monkeypatch.setattr(pathlib.Path, "is_dir", _broken_is_dir)
    result = measure(
        task_key="PRI-999", clone_path=str(tmp_path),
        changed_paths=["reviewer/web/api.py"], changed_status={"reviewer/web/api.py": "modified"},
    )
    assert result.status == "no_brief"


def test_measurement_carries_path_sets_not_only_counters(tmp_path):
    """Критерий 4 PRI-259: строка метрики хранит множества путей.

    Онлайн видит один PR, офлайн-baseline считался по задаче (union всех её
    PR). Без множеств путей union на чтении невозможен, и task-level число
    считалось бы другой линейкой, чем точка «до» (bulk_core_recall_median
    ≈ 0.373, bulk_n_measured = 4) — то есть сопоставимость молча исчезла бы.
    """
    clone = _clone(tmp_path)
    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=["reviewer/mcp/service.py", "reviewer/web/history.py"],
        changed_status={
            "reviewer/mcp/service.py": "modified",
            "reviewer/web/history.py": "modified",
        },
    )
    assert result.status == "measured"
    assert result.expected_core_paths        # не пусто
    assert result.hit_core_paths is not None
    for value in (result.expected_core_paths, result.hit_core_paths):
        assert not isinstance(value, int), "счётчик вместо множества путей ломает union"
        assert all(isinstance(path, str) for path in value)
    assert set(result.hit_core_paths) <= set(result.expected_core_paths)


def test_online_matches_offline_formula_on_full_diff(tmp_path):
    """Стык: онлайн (`measure`) и офлайн-формула (`recall.evaluate_task` по
    правилам `eval/solve_task_metrics/snapshot.py`) обязаны считать один и тот
    же вход одинаково — иначе линейка «до/после» несравнима (finding 1, 2).

    Вход намеренно содержит то, что раньше терялось в онлайне:
    - не-`.py` файл (`docs/readme.md`) — должен попасть в `expected`, но не в
      `expected_core`;
    - удалённый файл ядра (`reviewer/old/legacy.py`) — офлайн берёт diff целиком
      (`git diff --name-only`), значит `removed` обязан остаться в знаменателе;
    - переименованный файл ядра (`reviewer/new/renamed_target.py`) — офлайн
      проверяет `git cat-file -e <parent>:<path>`, для переименованного пути
      это False, значит он исключается из `expected_core` как «не существовал
      до PR».

    `changed_paths` намеренно уже отобранного review-подмножества (без
    удалённых и не-.py файлов) — измерение обязано игнорировать его в пользу
    полного `changed_status`.
    """
    brief_text = (
        "# Brief — PRI-999 Тестовая задача\n\n"
        "## Relevant code\n"
        "- `reviewer/mcp/service.py:1` — точка съёма\n"
        "- `reviewer/old/legacy.py:1` — устаревший код\n"
    )
    clone = _clone(tmp_path, text=brief_text)

    changed_status = {
        "reviewer/mcp/service.py": "modified",
        "reviewer/old/legacy.py": "removed",
        "reviewer/new/renamed_target.py": "renamed",
        "docs/readme.md": "modified",
        "tests/test_x.py": "added",
    }
    # То, что реально дошло бы до ревью после _select_changed_files: только
    # *.py, без removed, без docs/.
    review_selected_paths = [
        "reviewer/mcp/service.py",
        "reviewer/new/renamed_target.py",
        "tests/test_x.py",
    ]

    result = measure(
        task_key="PRI-999",
        clone_path=clone,
        changed_paths=review_selected_paths,
        changed_status=changed_status,
    )

    # Офлайн-формула (snapshot.py::build_snapshot), воспроизведённая явно на
    # том же входе: expected — весь diff, expected_core — ядро И существовал
    # до PR (`status not in {added, renamed, copied}`).
    predicted = {"reviewer/mcp/service.py", "reviewer/old/legacy.py"}
    expected_offline = set(changed_status)

    def existed_offline(path: str) -> bool:
        return changed_status.get(path) not in {"added", "renamed", "copied"}

    expected_core_offline = {
        path
        for path in expected_offline
        if classify.is_core_production_path(path, DEFAULT) and existed_offline(path)
    }
    offline_row = recall.evaluate_task(
        "PRI-999", predicted, expected_offline, expected_core_offline
    )

    assert result.status == "measured"
    assert result.expected == offline_row.expected == 5
    assert result.expected_core == offline_row.expected_core == 2
    assert result.hit_core == offline_row.hit_core == 2
    assert result.core_recall == pytest.approx(offline_row.core_recall) == pytest.approx(1.0)
    assert result.raw_recall == pytest.approx(offline_row.raw_recall) == pytest.approx(0.4)
    assert result.precision == pytest.approx(offline_row.precision) == pytest.approx(1.0)
    assert set(result.expected_core_paths) == {
        "reviewer/mcp/service.py",
        "reviewer/old/legacy.py",
    }
