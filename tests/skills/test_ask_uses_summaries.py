from pathlib import Path

ASK = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "ask" / "SKILL.md"


def test_ask_references_subsystem_summaries_prior():
    text = ASK.read_text(encoding="utf-8")
    assert "get_subsystem_summaries" in text


def test_ask_marks_prior_fail_open():
    text = ASK.read_text(encoding="utf-8").lower()
    assert "fail-open" in text and "get_subsystem_summaries" in ASK.read_text(encoding="utf-8")
