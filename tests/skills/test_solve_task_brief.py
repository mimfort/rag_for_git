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
