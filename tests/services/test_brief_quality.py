"""Тесты адаптера онлайн-метрики качества брифа (PRI-249).

Файловая система — только tmp_path; ни БД, ни git, ни сети.
"""
from __future__ import annotations

import pytest

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
