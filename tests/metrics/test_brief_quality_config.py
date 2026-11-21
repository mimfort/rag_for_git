import pytest

from reviewer.metrics.brief_quality.config import DEFAULT, BriefQualityConfig


@pytest.mark.parametrize("path", [
    "reviewer/app.py",                 # один сегмент — "**/" обязан матчить пустой путь
    "reviewer/index/store.py",
    "plugin/hooks/review_cost.py",
    "sync_chunk.py",                   # корневой *.py
])
def test_default_core_matches_production_paths(path):
    assert DEFAULT.matches_core(path) is True


@pytest.mark.parametrize("path", [
    "tests/metrics/test_classify.py",  # '*' не пересекает '/', значит tests/ вне ядра
    "docs/superpowers/plans/x.md",
    "plugin/skills/solve-task/SKILL.md",  # исключение "!plugin/**/*.md"
    "eval/solve_task_metrics/replay.py",
    "README.md",
])
def test_default_core_rejects_non_production_paths(path):
    assert DEFAULT.matches_core(path) is False


def test_foreign_repo_core_paths():
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": ["app/**/*.py", "frontend/src/**"]}}}
    )
    assert config.configured is True
    assert config.matches_core("app/api/routes.py") is True
    assert config.matches_core("frontend/src/pages/Login.tsx") is True
    assert config.matches_core("reviewer/app.py") is False


def test_absent_key_gives_default_and_unconfigured():
    config = BriefQualityConfig.from_review_yaml({})
    assert config.core_paths == DEFAULT.core_paths
    assert config.configured is False


def test_explicit_empty_list_is_configured():
    """Явный пустой список — высказывание «ядра нет», а не молчание."""
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": []}}}
    )
    assert config.core_paths == ()
    assert config.configured is True


def test_null_value_falls_back_to_default():
    """`core_paths:` без значения — YAML None, а не явный пустой список."""
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": None}}}
    )
    assert config.core_paths == DEFAULT.core_paths
    assert config.configured is False


def test_key_pattern_comes_from_task_board():
    config = BriefQualityConfig.from_review_yaml({"task_board": {"key_pattern": r"RON-\d+"}})
    assert config.key_pattern == r"RON-\d+"


def test_key_pattern_defaults_when_board_absent():
    assert BriefQualityConfig.from_review_yaml({}).key_pattern == r"[A-Z]+-\d+"
