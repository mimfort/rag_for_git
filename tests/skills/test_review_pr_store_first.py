from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "review-pr" / "SKILL.md")


def test_review_pr_reads_task_store_first():
    text = SKILL.read_text(encoding="utf-8")
    assert "get_task(" in text                       # store-first чтение из стора reviewer


def test_review_pr_board_mcp_is_fallback():
    text = SKILL.read_text(encoding="utf-8")
    # board-MCP плейбук — фолбэк только при промахе И заданном mcp
    assert "store-first" in text.lower() or "store first" in text.lower()
    assert "task_board.mcp" in text


def test_review_pr_youtrack_no_mcp_path():
    text = SKILL.read_text(encoding="utf-8")
    # пустой mcp (youtrack) → пропускаем requirements, не падаем
    assert "mcp" in text and "skip" in text.lower()
