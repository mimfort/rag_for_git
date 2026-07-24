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


def test_finish_task_reads_generic_done_target_and_options():
    t = SKILL.read_text(encoding="utf-8")
    assert "done_target" in t
    assert "provider_options" in t
    assert "targets" in t


def test_finish_task_names_resolved_done_target():
    t = SKILL.read_text(encoding="utf-8")
    assert "resolved done target" in t   # шаг 4 явно называет цель, не обобщённое mark done
    # гейт подтверждения не регрессирует
    assert "only after explicit confirmation" in t


def test_finish_task_uses_only_generic_board_metadata():
    text = SKILL.read_text(encoding="utf-8").lower()
    for token in ("create_target", "done_target", "options", "targets", "required_for", "choices"):
        assert token in text
    for forbidden in ("yougile", "youtrack", "done_column", "done_state", "status_field", "api_key"):
        assert forbidden not in text
