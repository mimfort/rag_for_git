"""Guardrail: solve-task фиксирует спеку brief + ранговый relevance-фильтр (PRI-146).

Шаг 4 SKILL.md должен нести: скелет-шаблон brief, колпаки top-3/top-5,
dropped-count и бинарное правило релевантности. Тест не пинит точные
формулировки — только стабильные маркеры спеки, чтобы правка не удалила её молча.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_solve_task_brief_spec_present():
    text = SOLVE.read_text(encoding="utf-8")
    assert "# Brief —" in text         # скелет-шаблон brief
    assert "≤3" in text                 # колпак related tasks
    assert "≤5" in text                 # колпак relevant code
    assert "(dropped" in text           # конвенция dropped-count
    assert "directly informs" in text   # бинарное правило релевантности


def test_solve_task_passes_project_scope():
    text = SOLVE.read_text(encoding="utf-8")
    assert "project=" in text
    assert "task_board.project" in text


def test_solve_task_persists_brief():
    """PRI-163: шаг персиста брифа в docs/superpowers/briefs/ + ссылка на путь в хендоффе."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "docs/superpowers/briefs/" in text   # целевой путь персиста
    assert "Persist the brief" in text          # шаг персиста присутствует
    assert "file path" in text                  # хендофф ссылается на путь к файлу
    assert "Board-less" in text                 # сохранение и без ключа (slug)


def test_solve_task_dedupes_related_sources():
    """PRI-164(b): «Related work» дедупится по ключу между linked и similar."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "Dedup related sources by key" in text   # явный шаг дедупа
    assert "linked ∪ similar" in text               # оба источника, слитые
    assert "canonical task key" in text             # дедуп по каноническому ключу


def test_solve_task_resolves_subtask_criteria_when_thin():
    """PRI-164(a): при тонком description критерии дорезолвятся из подзадач (fail-open, без index_task)."""
    text = SOLVE.read_text(encoding="utf-8")
    assert "Thin-criteria enrichment" in text                 # шаг присутствует
    assert "(?i)(критери|приёмк|acceptance)" in text          # детектор «тонкого» description
    assert "subtasks" in text                                  # источник критериев — подзадачи
    assert "do NOT call `index_task`" in text                  # обогащение только в бриф
