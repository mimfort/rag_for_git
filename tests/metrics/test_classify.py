"""Unit-тесты классификатора путей и таксономии промахов."""
import pytest

from reviewer.metrics.brief_quality import classify


@pytest.mark.parametrize(
    "path",
    ["reviewer/mcp/service.py", "plugin/hooks/brief_cost.py", "sync_chunk.py"],
)
def test_core_production_paths(path):
    assert classify.is_core_production_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "tests/mcp/test_service.py",
        "docs/superpowers/plans/x.md",
        "plugin/skills/review-pr/SKILL.md",
        "README.md",
        ".review.yml",
        "eval/run_eval.py",
        "docker-compose.yml",
    ],
)
def test_non_core_paths(path):
    assert classify.is_core_production_path(path) is False


def test_categorize_miss_new_file_wins_over_directory():
    assert classify.categorize_miss("reviewer/new.py", existed_before=False) == (
        classify.NEW_FILE_CATEGORY
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/mcp/test_x.py", "tests/"),
        ("docs/x.md", "docs/"),
        (".review.yml", ".review.yml/конфиги"),
        ("plugin/skills/ask/SKILL.md", "plugin/skills/*.md"),
        ("plugin/hooks/x.py", "plugin/ (прочее)"),
        ("reviewer/index/store.py", "reviewer/index"),
        ("eval/run_eval.py", "eval/"),
        ("Makefile", "прочее"),
    ],
)
def test_categorize_miss_existing_paths(path, expected):
    assert classify.categorize_miss(path, existed_before=True) == expected
