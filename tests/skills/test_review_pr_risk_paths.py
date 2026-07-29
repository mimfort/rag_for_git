import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "review-pr" / "SKILL.md")


def test_review_pr_dispatches_risk_dimension_conditionally():
    text = SKILL.read_text("utf-8")
    block = re.search(
        r"risk changes.*?(?=\n\s*- blast-radius:)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    assert block
    assert "ONLY if `risk_paths` is non-empty" in block.group()
    assert "risk-changes-prompt.md" in block.group()
    assert "risk_skipped_paths" in text
