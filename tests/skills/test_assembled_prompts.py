import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "plugin" / "skills"
_INCLUDE = re.compile(r"<!-- include: (\S+\.md) -->")


def assemble(rel_path: str) -> str:
    """Собрать промпт как оркестратор: подставить содержимое include-маркеров.

    Путь в маркере — относительно plugin/skills/. Резолв нерекурсивный
    (в _common-файлах маркеров нет).
    """
    text = (SKILLS_DIR / rel_path).read_text(encoding="utf-8")

    def repl(m: re.Match) -> str:
        return (SKILLS_DIR / m.group(1)).read_text(encoding="utf-8")

    out = _INCLUDE.sub(repl, text)
    assert "<!-- include:" not in out, f"неразрешённый include в {rel_path}"
    return out


def test_analyze_has_include_markers():
    raw = (SKILLS_DIR / "review-pr/references/analyze-prompt.md").read_text("utf-8")
    assert "<!-- include: _common/findings-schema.md -->" in raw
    assert "<!-- include: _common/anti-hallucination.md -->" in raw
    assert "<!-- include: _common/tool-usage.md -->" in raw


def test_analyze_assembled_has_all_rules():
    a = assemble("review-pr/references/analyze-prompt.md")
    assert "code_quote" in a                       # из findings-schema / anti-halluc
    assert '"confidence": 0.0' in a                # из findings-schema
    assert "empty findings list" in a.lower()      # из anti-hallucination
    assert "get_impact" in a                       # из tool-usage
    # analyze-специфичный хвост остался на месте
    assert "commentable_right" in a


def test_requirements_assembled_has_schema_and_category():
    r = assemble("review-pr/references/requirements-prompt.md")
    assert '"severity": "low|medium|high|critical"' in r
    assert 'category MUST be exactly "requirements"' in r  # фиксированная категория скилла


def test_blast_radius_assembled_has_tooling_and_confidence_tail():
    b = assemble("review-pr/references/blast-radius-prompt.md")
    assert "get_impact" in b
    assert "0.8" in b                              # confidence-scale хвост остался


def test_verify_keeps_verdicts_schema_and_tools():
    v = assemble("review-pr/references/verify-prompt.md")
    # PRI-156: verify теперь вызывает submit_verdicts, а не возвращает JSON-текст
    assert "submit_verdicts" in v                   # submit-контракт на месте
    assert "get_candidate_findings" in v            # чтение кандидатов тулом
    assert "find_callers" in v                      # tool-usage подставлен


def test_performance_assembled_schema_and_goal():
    p = assemble("performance-review/SKILL.md")
    assert '"category": "performance"' in p
    assert '"confidence": 0.0' in p                 # из findings-schema
    assert "N+1" in p                               # perf-специфичный хвост остался
    assert "Reviewer grounding (optional, fail-open)" in p   # reviewer-grounding подставлен
    assert "search_codebase" in p                   # session-less тул для standalone


def test_maintainability_assembled_schema_and_whatnot():
    m = assemble("maintainability-review/SKILL.md")
    assert '"confidence": 0.0' in m                 # из findings-schema
    assert "What Not To Flag" in m                  # maint-специфичный хвост остался
    assert "Reviewer grounding (optional, fail-open)" in m   # reviewer-grounding подставлен
    assert "search_codebase" in m                   # session-less тул для standalone


def test_ask_assembled_has_sessionless_tools_and_branch():
    a = assemble("ask/SKILL.md")
    assert "search_codebase" in a                   # session-less tool-usage
    assert "REVIEW_BRANCHES" in a                   # branch-selection
    assert "Grounding contract" in a                # ask-специфичный хвост остался


def test_solve_task_assembled_has_branch_and_tools():
    s = assemble("solve-task/SKILL.md")
    assert "REVIEW_BRANCHES" in s
    assert "search_codebase" in s
