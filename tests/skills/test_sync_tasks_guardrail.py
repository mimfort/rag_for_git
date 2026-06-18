from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "sync-tasks" / "SKILL.md"
REF = ROOT / "plugin" / "skills" / "sync-tasks" / "references" / "sync-tasks-yougile.md"


def test_skill_forbids_index_task_loop():
    text = SKILL.read_text(encoding="utf-8")
    assert "НИКОГДА" in text
    assert "index_task` в цикле" in text
    assert "index_tasks_batch" in text


def test_reference_forbids_index_task_loop():
    text = REF.read_text(encoding="utf-8")
    assert "index_task` в цикле" in text
    assert "index_tasks_batch" in text
