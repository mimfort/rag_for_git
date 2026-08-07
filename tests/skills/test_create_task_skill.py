"""Guardrail: скилл create-task — тонкий триггер server-side тула create_task."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "create-task" / "SKILL.md"
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_create_task_calls_write_tool_and_resyncs():
    t = SKILL.read_text(encoding="utf-8")
    assert "create_task(" in t
    assert "sync_board(" in t


def test_create_task_discovers_target_before_writing():
    t = SKILL.read_text(encoding="utf-8")
    assert "get_board_targets(" in t       # target — из discovery, не из головы
    assert "get_board_config()" in t       # фолбэк конфига доски
    assert "create_target" in t
    assert "required_for" in t
    assert "choices" in t


def test_create_task_confirms_and_noops_boardless():
    t = SKILL.read_text(encoding="utf-8").lower()
    assert "confirm" in t                  # никогда не пишет молча
    assert "board-less" in t or "no-op" in t


def test_create_task_grounds_body_in_code():
    t = SKILL.read_text(encoding="utf-8")
    assert "search_codebase(" in t         # «Проблема» ссылается на path:line
    assert "path:line" in t


def test_create_task_forbids_decorative_output():
    t = SKILL.read_text(encoding="utf-8").lower()
    assert "no emoji" in t


def test_create_task_answers_in_russian():
    assert "Russian" in SKILL.read_text(encoding="utf-8")


def test_solve_task_points_to_create_task():
    assert "create-task" in SOLVE.read_text(encoding="utf-8")


def test_create_task_uses_only_generic_board_metadata():
    text = SKILL.read_text(encoding="utf-8").lower()
    for token in ("create_target", "done_target", "options", "targets", "required_for", "choices"):
        assert token in text
    for forbidden in ("yougile", "youtrack", "done_column", "done_state", "status_field", "api_key"):
        assert forbidden not in text
