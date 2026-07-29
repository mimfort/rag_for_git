from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "plugin" / "skills"
COMMON = SKILLS_DIR / "_common"


def _read(name: str) -> str:
    return (COMMON / name).read_text(encoding="utf-8")


def test_all_common_files_exist_nonempty():
    for name in (
        "findings-schema.md",
        "anti-hallucination.md",
        "tool-usage.md",
        "reviewer-grounding.md",
        "branch-selection.md",
        "dimension-scope.md",
        "dimension-output-tail.md",
    ):
        assert (COMMON / name).is_file(), f"нет {name}"
        assert len(_read(name).strip()) > 0, f"{name} пустой"


def test_common_files_have_no_include_markers():
    # Резолвер include нерекурсивный (test_assembled_prompts.assemble подставляет
    # за один проход), поэтому _common-файлы не должны содержать include-маркеров.
    for path in sorted(COMMON.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert "<!-- include:" not in text, f"{path.name} содержит include-маркер"


def test_findings_schema_matches_finding_dataclass():
    # Каждое публичное поле Finding должно присутствовать в схеме.
    from reviewer.vcs.base import Finding
    import dataclasses

    schema = _read("findings-schema.md")
    field_to_token = {
        "category": "category",
        "severity": "severity",
        "file": "file",
        "line": "line",
        "side": "side",
        "message": "message",
        "suggestion": "suggestion",
        "confidence": "confidence",
        "code_quote": "code_quote",
        # fix_start/fix_end/replacement сворачиваются в JSON-объект "fix"
        "fix_start": "fix",
        "fix_end": "fix",
        "replacement": "replacement",
    }
    for f in dataclasses.fields(Finding):
        token = field_to_token.get(f.name)
        if token is None:
            continue
        assert token in schema, f"поле {f.name} (токен {token}) отсутствует в findings-schema.md"


def test_anti_hallucination_has_core_principles():
    text = _read("anti-hallucination.md").lower()
    assert "code_quote" in text
    assert "hallucinat" in text          # «a hallucinated absence is worse…»
    assert "empty findings list" in text  # пустой список — валидный результат


def test_tool_usage_has_both_tool_families():
    text = _read("tool-usage.md")
    # PR-session
    assert "search_code" in text and "get_changed_file_diff" in text and "get_impact" in text
    # session-less
    assert "search_codebase" in text and "related_symbols" in text and "definition" in text
    # PRI-156: submit-тулы schema-enforced вывода
    assert "submit_findings" in text and "submit_verdicts" in text
    assert "including files changed by the PR" in text


def test_findings_schema_matches_finding_in_model():
    # PRI-156: findings-schema.md — проекция канона FindingIn.
    from reviewer.mcp.schemas import FindingIn

    schema = _read("findings-schema.md")
    for name in FindingIn.model_fields:
        token = "fix" if name == "fix" else name
        assert token in schema, f"поле FindingIn.{name} отсутствует в findings-schema.md"


def test_branch_selection_has_review_branches_logic():
    text = _read("branch-selection.md")
    assert "REVIEW_BRANCHES" in text
    assert "git branch --show-current" in text


def test_reviewer_grounding_has_core_rules():
    text = _read("reviewer-grounding.md")
    assert "Reviewer grounding (optional, fail-open)" in text  # заголовок блока
    assert "reviewer status" in text and "drift == 0" in text  # freshness-check
    # session-less тулы перечислены
    assert "search_codebase" in text and "callers" in text \
        and "related_symbols" in text and "definition" in text
    assert "grep" in text.lower()                              # fail-open в grep
    assert "3 RPM" in text                                     # политика «точечно» (Voyage rate-limit)
    assert "base:<branch>" in text                             # честность WIP vs base
    assert "<!-- include:" not in text                         # без вложенных include
