from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_readme_en_has_grounding_section():
    text = _read("README.md")
    assert "## Reviewer grounding in plan/review phases (optional)" in text
    assert "Reviewer grounding (plan/review, optional, fail-open)" in text  # копипаст-блок
    assert "search_codebase" in text and "callers" in text
    assert "drift == 0" in text
    assert "[Reviewer grounding in plan/review phases]" in text             # запись в ToC


def test_readme_ru_has_grounding_section():
    text = _read("README.ru.md")
    assert "## Грунтовка reviewer в фазах план/ревью (опционально)" in text
    assert "Грунтовка reviewer (план/ревью, опционально, fail-open)" in text  # копипаст-блок
    assert "search_codebase" in text and "callers" in text


def test_plugin_readme_points_to_grounding():
    text = _read("plugin/README.md")
    assert "грунтов" in text.lower()  # указатель на раздел грунтовки


def test_readmes_point_to_authoritative_board_provider_reference():
    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        assert "docs/board-providers.md" in text
        assert "provider_options" in text


def test_readmes_keep_generic_task_key_metadata_in_examples():
    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        assert "key_pattern:" in text
        assert "url_template:" in text
