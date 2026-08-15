from pathlib import Path

from .test_assembled_prompts import assemble

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


def _solve() -> str:
    return assemble("solve-task/SKILL.md")


def test_solve_task_passes_query_to_summaries():
    text = _solve()
    assert "get_subsystem_summaries(repo, branch, query=" in text


def test_solve_task_has_subsystems_brief_section():
    text = _solve()
    assert "## Subsystems" in text


def test_solve_task_marks_summary_prior_only():
    # Приор сводок — только ориентир: grounding (path:line) идёт из search_codebase,
    # а не из текста summary (зеркало ask/SKILL.md). Критерий приёмки PRI-161.
    text = _solve()
    assert "never from the summary text" in text


def test_ask_lazy_expansion_present():
    """PRI-202: ленивый перевызов search_codebase с большим top_k под cliff/rails-хвост."""
    text = ASK.read_text(encoding="utf-8")
    assert "Lazy expansion (no user prompt)" in text  # шаг присутствует, без интеррапта
    assert "top_k=" in text                           # перевызов с большим потолком
