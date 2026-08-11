"""Guardrail: скилл report-bug — тонкий триггер server-side канала (PRI-239).

Позитивные ассерты фиксируют формулировки контракта, негативные — то, чего в промпте
быть не должно: молчаливую публикацию и ручное «подчищу текст сам».
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "report-bug" / "SKILL.md"
COMMON = ROOT / "plugin" / "skills" / "_common" / "bug-reporting.md"
WIRED_SKILLS = ("review-pr", "solve-task", "summarize-subsystems")


def _skill() -> str:
    """Текст скилла со схлопнутыми переносами: ассерты — про формулировку, не про вёрстку."""
    return " ".join(SKILL.read_text(encoding="utf-8").split())


def test_skill_calls_the_server_tool():
    assert "report_bug(" in _skill()


def test_publication_requires_an_explicit_approval():
    text = _skill()
    assert "confirmed=True" in text
    assert "explicit yes" in text.lower()
    assert "Never publish autonomously in any mode." in text


def test_full_issue_text_is_shown_before_approval():
    text = _skill()
    assert "in full, verbatim" in text
    assert "no summary, no" in text


def test_headless_runs_use_the_non_interactive_flag():
    text = _skill()
    assert "non_interactive=True" in text
    assert "headless" in text


def test_skill_forbids_hand_redaction_and_pasting_source():
    text = _skill().lower()
    assert "never pre-redact by hand" in text
    assert "never paste source" in text


def test_skill_lists_both_sides_of_the_filter():
    text = _skill()
    for kind in ("tool_exception", "contract_violation", "skill_impossible",
                 "skill_contradiction", "invariant_violation", "deterministic_repro"):
        assert kind in text
    for kind in ("environment", "external_service", "user_code", "permission",
                 "llm_behaviour"):
        assert kind in text
    assert "caused_by_skill_instruction=True" in text


def test_skill_covers_every_severity_and_the_defer_rule():
    text = _skill()
    for level in ("blocker", "degraded", "contract"):
        assert level in text
    assert "defer=true" in text


def test_skill_handles_every_terminal_status():
    text = _skill()
    for status in ("disabled", "not_reported", "suppressed", "published",
                   "commented", "fallback"):
        assert status in text
    assert "decline=True" in text


def test_skill_warns_about_the_public_username():
    assert "identity_notice" in _skill()


def test_skill_offers_environment_trimming():
    text = _skill()
    assert "environment_exclude" in text
    assert "environment_include" in text


def test_common_block_exists_and_separates_ours_from_foreign():
    text = COMMON.read_text(encoding="utf-8")
    assert "report-bug" in text
    assert "Stay silent" in text
    assert "<!-- include:" not in text        # резолвер include нерекурсивный


def test_common_block_is_wired_into_the_defect_prone_skills():
    for name in WIRED_SKILLS:
        text = (ROOT / "plugin" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert "<!-- include: _common/bug-reporting.md -->" in text, name
