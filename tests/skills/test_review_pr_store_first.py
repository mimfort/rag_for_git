import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "review-pr" / "SKILL.md")


def test_review_pr_reads_task_store_first():
    text = SKILL.read_text(encoding="utf-8")
    assert "get_task(" in text                       # store-first чтение из стора reviewer


def test_review_pr_generic_miss_syncs_then_retries_store():
    text = SKILL.read_text(encoding="utf-8")
    assert "store-first" in text.lower() or "store first" in text.lower()
    miss = re.search(r"\*\*Miss\*\*.*?(?=\n\s*The `TaskBrief` schema)", text, re.DOTALL)
    assert miss, "нужна явная ветка miss после store-first"
    assert "sync_board(" in miss.group()
    assert "get_task(" in miss.group()
    assert "fail-open" in miss.group().lower()


def test_review_pr_task_context_is_generic_and_has_no_playbooks():
    text = SKILL.read_text(encoding="utf-8")
    assert "task_board.mcp" not in text
    assert "task-context-" not in text
    assert not (SKILL.parent / "references" / "task-context-jira.md").exists()
    assert not (SKILL.parent / "references" / "task-context-yougile.md").exists()


def test_review_pr_task_board_payload_uses_generic_metadata_only():
    text = SKILL.read_text(encoding="utf-8")
    payload = re.search(r"- `task_board`: .*?(?=\n\s*- `task_keys`)", text, re.DOTALL)
    assert payload
    for field in ("type", "project", "key_pattern", "create_target", "done_target", "options"):
        assert field in payload.group()


def test_solve_task_resolves_board_once_before_preflight_sync():
    solve = (SKILL.parents[1] / "solve-task" / "SKILL.md").read_text(encoding="utf-8")
    # Заголовок Шага 0 переименован в PRI-243 (стартовый опрос + preflight); якорь ловит
    # оба варианта, но требует, чтобы слово "Preflight" осталось в заголовке.
    preflight = re.search(r"0\. \*\*[^\n]*Preflight.*?(?=\n1\. \*\*Config\.)", solve, re.DOTALL)
    assert preflight
    assert "Resolve `task_board` exactly once" in preflight.group()
    assert preflight.group().index("Resolve `task_board` exactly once") < preflight.group().index("sync_board(")
    for branch in ("repo `.review.yml`", "get_board_config()", "empty `task_board:`", "reuse this resolved value"):
        assert branch in preflight.group()
