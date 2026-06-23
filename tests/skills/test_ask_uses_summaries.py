from pathlib import Path

ASK = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "ask" / "SKILL.md"
PRW = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "pr-walkthrough" / "SKILL.md"
SUMM = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "summarize-subsystems" / "SKILL.md"


def test_ask_references_subsystem_summaries_prior():
    text = ASK.read_text(encoding="utf-8")
    assert "get_subsystem_summaries" in text


def test_ask_marks_prior_fail_open():
    text = ASK.read_text(encoding="utf-8").lower()
    assert "fail-open" in text and "get_subsystem_summaries" in ASK.read_text(encoding="utf-8")


def test_ask_passes_query_to_summaries():
    text = ASK.read_text(encoding="utf-8")
    assert "get_subsystem_summaries(repo, branch, query=" in text


def test_pr_walkthrough_passes_query_to_summaries():
    text = PRW.read_text(encoding="utf-8")
    assert "get_subsystem_summaries(repo, pr.base_ref, query=" in text


def test_summarize_triggers_embedding_backfill():
    text = SUMM.read_text(encoding="utf-8")
    assert "backfill_summary_embeddings" in text
