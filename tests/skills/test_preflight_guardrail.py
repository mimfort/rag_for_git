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


def test_ask_has_warn_only_freshness():
    text = ASK.read_text(encoding="utf-8")
    assert "--json" in text and "reviewer status" in text   # читает машиночитаемый статус
    assert "отстаёт на" in text                             # warn-баннер про дрейф (рус.)
    assert "reviewer_sync-codebase" in text                 # баннер указывает на reindex-скил
    # облегчённый режим: НЕ зовёт sync_board и не реиндексирует
    assert "sync_board(" not in text
