"""Guardrail: скилы solve-task и ask проверяют свежесть base-индекса (PRI-141).

solve-task — блокирующий Step 0 Preflight (drift → подтверждение → reindex,
+ sync_board прогрев корпуса задач). ask — облегчённый warn-only баннер,
без sync_board/reindex/блокировки.
"""
from pathlib import Path

from .test_assembled_prompts import assemble

ROOT = Path(__file__).resolve().parents[2]
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"
ASK = ROOT / "plugin" / "skills" / "ask" / "SKILL.md"


def _solve() -> str:
    return assemble("solve-task/SKILL.md")


def test_solve_task_has_preflight():
    text = _solve()
    assert "Preflight" in text                              # Step 0 добавлен
    assert "reviewer status" in text and "--json" in text   # читает машиночитаемый статус
    assert "drift" in text                                  # проверяет дрейф
    assert "sync_board(" in text                            # прогрев корпуса задач
    assert "rag-reviewer:sync-codebase" in text             # делегирование reindex


def test_ask_has_warn_only_freshness():
    text = ASK.read_text(encoding="utf-8")
    assert "--json" in text and "reviewer status" in text   # читает машиночитаемый статус
    assert "отстаёт на" in text                             # warn-баннер про дрейф (рус.)
    assert "rag-reviewer:sync-codebase" in text             # баннер указывает на reindex-скил
    # облегчённый режим: НЕ зовёт sync_board и не реиндексирует
    assert "sync_board(" not in text


def test_solve_task_preflight_passes_board_type():
    text = _solve()
    # preflight sync_board должен передавать board_type из task_board.type
    assert "board_type" in text


def _summary_warmth_section() -> str:
    """Вырезать пункт 4 preflight'а solve-task — от заголовка до блока Decisions."""
    text = _solve()
    start = text.index("4. **Summary warmth.**")
    return text[start:text.index("Decisions:", start)]


def test_solve_task_reads_summaries_from_status():
    # теплота сводок берётся из payload'а status, полученного в Step 0.1 — а не отдельным
    # вызовом тула; формулировки специфичны для нового текста (в старом их не было)
    section = _summary_warmth_section()
    assert "Step 0.1 status payload" in section
    assert "do NOT probe" in section


def test_solve_task_probes_summaries_only_as_fallback():
    # единственное упоминание тула — фолбэк для деплоя старше поля summaries
    assert _summary_warmth_section().count("get_subsystem_summaries") == 1


def test_solve_task_keeps_three_warmth_options():
    section = _summary_warmth_section()
    assert "Прогреть сейчас" in section
    assert "Прогрею сам" in section
    assert "Пропустить" in section
