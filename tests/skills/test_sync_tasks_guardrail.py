"""Guardrail: скилл sync-tasks — тонкий триггер server-side тула sync_board.

После PRI-140 enumerate/normalize/index выполняются на сервере; скилл лишь зовёт
sync_board и печатает summary. Никакого LLM-обхода доски и поштучной индексации.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "sync-tasks" / "SKILL.md"
OLD_REF = ROOT / "plugin" / "skills" / "sync-tasks" / "references" / "sync-tasks-yougile.md"


def test_skill_is_thin_trigger_of_sync_board():
    text = SKILL.read_text(encoding="utf-8")
    assert "sync_board(" in text                 # вызывает серверный тул
    assert "server-side" in text                 # явно: работа на сервере
    # LLM больше не индексирует поштучно и не вызывает index_tasks_batch из скилла
    assert "index_tasks_batch" not in text
    assert "index_task(" not in text


def test_legacy_enumeration_reference_removed():
    assert not OLD_REF.exists()
