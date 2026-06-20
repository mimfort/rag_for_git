"""Guardrail: скилы solve-task и ask проверяют свежесть base-индекса (PRI-141).

solve-task — блокирующий Step 0 Preflight (drift → подтверждение → reindex,
+ sync_board прогрев корпуса задач). ask — облегчённый warn-only баннер,
без sync_board/reindex/блокировки.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"
ASK = ROOT / "plugin" / "skills" / "ask" / "SKILL.md"


def test_solve_task_has_preflight():
    text = SOLVE.read_text(encoding="utf-8")
    assert "Preflight" in text                              # Step 0 добавлен
    assert "reviewer status" in text and "--json" in text   # читает машиночитаемый статус
    assert "drift" in text                                  # проверяет дрейф
    assert "sync_board(" in text                            # прогрев корпуса задач
    assert "reviewer_sync-codebase" in text                 # делегирование reindex
