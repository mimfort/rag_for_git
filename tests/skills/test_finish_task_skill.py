"""Guardrail: скилл finish-task — тонкий триггер server-side тула finish_task."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "finish-task" / "SKILL.md"
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_finish_task_name_follows_reviewer_prefix():
    # Все скиллы плагина инвокаются как /reviewer_<name>; кросс-ссылки зовут /reviewer_finish-task.
    assert "name: reviewer_finish-task" in SKILL.read_text(encoding="utf-8")


def test_finish_task_calls_write_tool_and_resyncs():
    t = SKILL.read_text(encoding="utf-8")
    assert "finish_task(" in t          # зовёт серверный write-тул
    assert "sync_board(" in t           # ре-индекс закрытой задачи после записи


def test_finish_task_confirms_and_noops_boardless():
    t = SKILL.read_text(encoding="utf-8").lower()
    assert "confirm" in t                # никогда не пишет молча
    assert "board-less" in t or "no-op" in t   # graceful no-op без ключа/доски


def test_finish_task_resolves_key_and_pr():
    t = SKILL.read_text(encoding="utf-8")
    assert "key_pattern" in t            # резолв ключа по паттерну
    assert "briefs" in t                 # восстановление ключа из брифа
    assert "gh pr view" in t             # резолв pr_url


def test_solve_task_points_to_finish_task():
    assert "finish-task" in SOLVE.read_text(encoding="utf-8")
