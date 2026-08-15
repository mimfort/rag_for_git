import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "plugin" / "skills"
_INCLUDE = re.compile(r"<!-- include: ([A-Za-z0-9_\-/]+\.md) -->")
_PROVIDER_SPECIFIC = re.compile(
    r"(yougile|youtrack|jira)|done_column|done_state|status_field|task_board\.mcp|task-context-",
    re.IGNORECASE,
)


def assemble(rel_path: str, _stack: tuple = ()) -> str:
    """Собрать промпт как оркестратор: подставить содержимое include-маркеров.

    Путь в маркере — относительно plugin/skills/. Резолв рекурсивный:
    SKILL.md включает references/*.md, те в свою очередь включают _common/*.md.
    Цикл включений — ошибка сборки, а не бесконечная рекурсия.
    """
    assert rel_path not in _stack, f"цикл включений: {' -> '.join((*_stack, rel_path))}"
    text = (SKILLS_DIR / rel_path).read_text(encoding="utf-8")

    def repl(m: re.Match) -> str:
        return assemble(m.group(1), (*_stack, rel_path))

    out = _INCLUDE.sub(repl, text)
    assert not _INCLUDE.search(out), f"неразрешённый include в {rel_path}"
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


def test_risk_changes_assembled_has_schema_and_evidence_guards():
    prompt = assemble("review-pr/references/risk-changes-prompt.md")
    assert '"severity": "low|medium|high|critical"' in prompt
    assert "submit_findings" in prompt
    assert "path or reason is not evidence" in prompt
    assert "ordinary configuration" in prompt
    assert "do not repeat a credential value" in prompt
    assert "commentable_right" in prompt


def test_risk_changes_removed_file_exception_overrides_shared_new_file_rules():
    prompt = assemble("review-pr/references/risk-changes-prompt.md")
    shared_new_only = "code_quote` — exact line copied verbatim from the NEW file"
    exception = "Removed-file exception"

    assert prompt.index(exception) > prompt.index(shared_new_only)
    removed_rules = prompt[prompt.index(exception):]
    assert "old-file source" in removed_rules
    assert "exact deleted line" in removed_rules
    assert "`side: LEFT`" in removed_rules
    assert "`commentable_left`" in removed_rules
    assert "`line: null`" in removed_rules


def test_blast_radius_assembled_has_tooling_and_confidence_tail():
    b = assemble("review-pr/references/blast-radius-prompt.md")
    assert "get_impact" in b
    assert "0.8" in b                              # confidence-scale хвост остался
    # interface expansion (PRI-206): триггер + секция + lower-bound фрейминг
    assert "Interface expansion" in b             # новая секция измерения
    assert "Protocol" in b                         # триггер интерфейс-правки
    assert "abstract" in b.lower()                 # ABC / abstractmethod триггер
    assert "all implementations are covered" in b   # guard: fail-open lower-bound для interface-expansion


def test_verify_keeps_verdicts_schema_and_tools():
    v = assemble("review-pr/references/verify-prompt.md")
    # PRI-156: verify теперь вызывает submit_verdicts, а не возвращает JSON-текст
    assert "submit_verdicts" in v                   # submit-контракт на месте
    assert "get_candidate_findings" in v            # чтение кандидатов тулом
    assert "find_callers" in v                      # tool-usage подставлен


def test_verify_prompt_instructs_reject_reason():
    v = assemble("review-pr/references/verify-prompt.md")
    lowered = v.lower()
    # каждый assert должен падать, если убрать новую инструкцию про reason:
    # "reason" появился в промпте только вместе с этой правкой (submit_verdicts
    # и новый абзац), а две следующие фразы уникальны именно для нового абзаца
    # (не встречаются больше нигде в файле — проверено grep'ом).
    assert "reason" in lowered
    assert "must include a short" in lowered
    assert "naming which rule fired" in lowered


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


def test_public_skill_markdown_has_no_provider_specific_board_surface():
    offenders = []
    for path in SKILLS_DIR.rglob("*.md"):
        match = _PROVIDER_SPECIFIC.search(path.read_text(encoding="utf-8"))
        if match:
            offenders.append(f"{path.relative_to(SKILLS_DIR)}: {match.group()}")

    assert not offenders, "provider-specific board surface leaked:\n" + "\n".join(offenders)


def test_no_public_task_context_playbooks_remain():
    assert not list(SKILLS_DIR.rglob("task-context-*.md"))


def test_assemble_resolves_nested_reference_includes(tmp_path, monkeypatch):
    """Маркер может указывать на references/*.md, который сам включает _common/*.md."""
    import tests.skills.test_assembled_prompts as mod

    root = tmp_path / "skills"
    (root / "_common").mkdir(parents=True)
    (root / "demo" / "references").mkdir(parents=True)
    (root / "_common" / "leaf.md").write_text("ЛИСТ", encoding="utf-8")
    (root / "demo" / "references" / "mid.md").write_text(
        "СЕРЕДИНА\n<!-- include: _common/leaf.md -->\n", encoding="utf-8")
    (root / "demo" / "SKILL.md").write_text(
        "КОРЕНЬ\n<!-- include: demo/references/mid.md -->\n", encoding="utf-8")

    monkeypatch.setattr(mod, "SKILLS_DIR", root)
    out = mod.assemble("demo/SKILL.md")
    assert "КОРЕНЬ" in out and "СЕРЕДИНА" in out and "ЛИСТ" in out


def test_assemble_detects_include_cycle(tmp_path, monkeypatch):
    import pytest

    import tests.skills.test_assembled_prompts as mod

    root = tmp_path / "skills"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "a.md").write_text("<!-- include: demo/b.md -->", encoding="utf-8")
    (root / "demo" / "b.md").write_text("<!-- include: demo/a.md -->", encoding="utf-8")
    monkeypatch.setattr(mod, "SKILLS_DIR", root)
    with pytest.raises(AssertionError, match="цикл"):
        mod.assemble("demo/a.md")
